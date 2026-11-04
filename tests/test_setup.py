"""First-run setup: FFmpeg and a Meta API key, and nothing to download."""

from __future__ import annotations

import http.client
import json
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from vedit import doctor, onboarding, settings
from vedit.studio.server import StudioHandler, _LoopbackServer

KEY = "test-meta-key-0123456789"


@pytest.fixture
def meta_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")


@pytest.fixture
def no_meta(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)


def test_setup_has_no_speech_model_or_download() -> None:
    # Every model Briefcut calls is a Meta model. Setup fetches nothing.
    for gone in ("downloader", "ModelDownload", "MODEL_URL", "speech_status", "models_dir", "wait_for"):
        assert not hasattr(onboarding, gone), gone
    for gone in ("whisper_install", "suggest_whisper_model"):
        assert not hasattr(doctor, gone), gone


def test_the_meta_key_is_required_and_says_where_to_get_one(no_meta: None) -> None:
    item = item_named(onboarding.setup_status(), "meta")
    assert item["required"] is True and item["ok"] is False and item["key_configured"] is False
    assert item["key_url"] == "https://dev.meta.ai"
    assert "Meta Voice Transcribe" in item["why"]
    assert onboarding.setup_status()["ready"] is False


def test_a_key_without_allowing_calls_is_not_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", KEY)
    monkeypatch.setenv("VEDIT_META_ENABLED", "0")
    item = item_named(onboarding.setup_status(), "meta")
    assert item["ok"] is False and item["key_configured"] is True
    assert "Turn on Meta" in item["summary"]


def test_doctor_requires_meta(no_meta: None) -> None:
    check = doctor.meta_check()
    assert check.name == "meta" and check.required is True and check.ok is False
    assert "https://dev.meta.ai" in check.detail and KEY not in check.detail


def test_doctor_meta_check_passes_with_a_key_and_never_prints_it(meta_ready: None) -> None:
    check = doctor.meta_check()
    assert check.ok is True and check.required is True
    assert KEY not in check.detail and KEY[-4:] not in check.detail


def test_doctor_has_no_whisper_row(meta_ready: None) -> None:
    names = [check.name for check in doctor.run_doctor()]
    assert "whisper-cli" not in names


def test_cli_setup_points_to_the_key_page(no_meta: None, capsys: pytest.CaptureFixture[str]) -> None:
    from vedit.cli import main

    assert main(["setup"]) == 1
    out = capsys.readouterr().out
    assert "https://dev.meta.ai" in out and "whisper" not in out.lower() and "speech model" not in out.lower()
    with pytest.raises(SystemExit):
        main(["setup", "--speech-model"])


def http_call(port: int, method: str, path: str, payload: object = None, headers: dict[str, str] | None = None) -> tuple[int, dict]:
    outgoing = dict(headers or {})
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        outgoing.setdefault("Content-Type", "application/json")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(method, path, body=body, headers=outgoing)
        response = connection.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        connection.close()


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MODEL_API_KEY", raising=False)
    monkeypatch.delenv("VEDIT_META_ENABLED", raising=False)
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=3)


def fake_ffmpeg(folder: Path, *filters: str, encoders: tuple[str, ...] = ("libx264", "aac")) -> Path:
    """An ffmpeg and ffprobe that list only the filters and encoders they are given."""
    folder.mkdir(parents=True, exist_ok=True)
    listing = "\\n".join(f" .. {name} V->V x" for name in filters)
    encoder_listing = "\\n".join(f" V..... {name} x" for name in encoders)
    tool = folder / "ffmpeg"
    tool.write_text(f"""#!/bin/sh
case "$*" in
  *-filters*) printf '{listing}\\n' ;;
  *-encoders*) printf '{encoder_listing}\\n' ;;
  *) echo 'ffmpeg version test' ;;
esac
""", encoding="utf-8")
    tool.chmod(0o755)
    (folder / "ffprobe").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (folder / "ffprobe").chmod(0o755)
    return tool


@pytest.fixture
def clear_tool_cache() -> Iterator[None]:
    onboarding.refresh_tools()
    yield
    onboarding.refresh_tools()


def item_named(status: dict, name: str) -> dict:
    return next(row for row in status["items"] if row["id"] == name)


def test_setup_is_ready_with_a_full_ffmpeg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_tool_cache: None, meta_ready: None) -> None:
    monkeypatch.setenv("VEDIT_FFMPEG", str(fake_ffmpeg(tmp_path / "ff", *onboarding.STUDIO_FILTERS)))
    status = onboarding.setup_status(refresh=True)
    assert status["ready"] is True
    assert item_named(status, "ffmpeg")["ok"] is True


def test_setup_names_missing_filters_and_gives_the_command(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_tool_cache: None) -> None:
    monkeypatch.setenv("VEDIT_FFMPEG", str(fake_ffmpeg(tmp_path / "ff", "silencedetect", "loudnorm")))
    status = onboarding.setup_status(refresh=True)
    item = item_named(status, "ffmpeg")
    assert status["ready"] is False and item["ok"] is False
    assert "drawtext" in item["summary"] and "subtitles" in item["summary"]
    assert item["install"]["steps"]


def test_setup_needs_an_h264_encoder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_tool_cache: None) -> None:
    monkeypatch.setenv("VEDIT_FFMPEG", str(fake_ffmpeg(tmp_path / "ff", *onboarding.STUDIO_FILTERS, encoders=("aac",))))
    status = onboarding.setup_status(refresh=True)
    assert status["ready"] is False and "libx264" in item_named(status, "ffmpeg")["summary"]


def test_setup_with_no_ffmpeg_gives_the_command_and_does_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_tool_cache: None) -> None:
    monkeypatch.setenv("VEDIT_FFMPEG", str(tmp_path / "missing" / "ffmpeg"))
    status = onboarding.setup_status(refresh=True)
    item = item_named(status, "ffmpeg")
    assert status["ready"] is False and "not installed" in item["summary"]
    assert any("ffmpeg" in step["command"] for step in item["install"]["steps"])


def test_check_again_sees_a_fresh_install_without_a_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clear_tool_cache: None, meta_ready: None) -> None:
    tool = fake_ffmpeg(tmp_path / "ff", "loudnorm")
    monkeypatch.setenv("VEDIT_FFMPEG", str(tool))
    assert onboarding.setup_status(refresh=True)["ready"] is False
    fake_ffmpeg(tmp_path / "ff", *onboarding.STUDIO_FILTERS)
    assert onboarding.setup_status()["ready"] is False, "still the cached answer"
    assert onboarding.setup_status(refresh=True)["ready"] is True


def test_macos_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "package_manager", lambda: "brew")
    assert [step["command"] for step in doctor.ffmpeg_install()["steps"]] == ["brew install ffmpeg-full"]


def test_macos_without_homebrew_starts_with_homebrew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(doctor, "package_manager", lambda: None)
    steps = doctor.ffmpeg_install()["steps"]
    assert "Homebrew" in steps[0]["label"] and steps[-1]["command"] == "brew install ffmpeg-full"


def test_linux_commands_never_mention_homebrew(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(doctor, "package_manager", lambda: "apt")
    ffmpeg = doctor.ffmpeg_install()["steps"]
    assert "apt-get install -y ffmpeg" in ffmpeg[0]["command"]
    assert "brew" not in " ".join(step["command"] for step in ffmpeg)


def test_linux_without_a_known_manager_says_what_is_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(doctor, "package_manager", lambda: None)
    install = doctor.ffmpeg_install()
    assert install["steps"] == [] and "libx264" in install["note"]


def test_linux_capabilities_are_honest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    host = onboarding.host_capabilities()
    assert host == {"platform": "linux", "hardware_encoder": False, "keychain": False, "native_app": False}


def test_capabilities_carry_the_host_and_advertise_no_local_captions(studio: int) -> None:
    status, body = http_call(studio, "GET", "/api/capabilities")
    assert status == 200
    assert body["host"]["platform"] in {"macos", "linux"}
    assert "local" not in body and set(body["render"]) == {"ready", "reason"}
    assert body["meta"]["models"]["transcribe"], "captions are Meta Voice Transcribe"
    assert "whisper" not in json.dumps(body).lower()


def test_setup_route_shape(studio: int) -> None:
    status, body = http_call(studio, "GET", "/api/setup")
    assert status == 200 and body["ok"] is True
    assert [item["id"] for item in body["items"]] == ["ffmpeg", "meta"]
    assert body["checks"][0]["name"] == "python", "the doctor's own rows come along"
    assert body["ready"] is False, "no Meta key in this test"
    assert all(item["required"] for item in body["items"])
    assert "whisper" not in json.dumps(body).lower()


@pytest.mark.parametrize(("method", "path", "payload"), [
    ("GET", "/api/setup", None), ("POST", "/api/settings", {}), ("GET", "/api/diagnostics", None),
])
@pytest.mark.parametrize("headers", [{"Host": "evil.example"}, {"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}])
def test_setup_routes_refuse_other_origins(studio: int, method: str, path: str, payload: object, headers: dict[str, str]) -> None:
    status, _body = http_call(studio, method, path, payload, headers)
    assert status == 403


@pytest.mark.parametrize(("method", "path"), [("GET", "/api/setup/model"), ("POST", "/api/setup/model"), ("POST", "/api/setup/model/cancel")])
def test_the_model_download_route_is_gone(studio: int, method: str, path: str) -> None:
    status, _body = http_call(studio, method, path, {} if method == "POST" else None)
    assert status == 404


def test_unknown_routes_stay_not_found(studio: int) -> None:
    assert http_call(studio, "GET", "/api/setup/nope")[0] == 404
    assert http_call(studio, "POST", "/api/projects/nope", {})[0] == 404


# ---- Diagnostics ----


def test_diagnostics_hold_no_paths_keys_or_transcript(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    key = "sk-diagnostic-key-123456"
    monkeypatch.setenv("MODEL_API_KEY", key)
    home = str(Path.home())
    job = {
        "status": "failed", "stage": "failed", "revision": 2, "request": {"provider": "meta", "goal": "A secret client brief", "paths": [f"{home}/Movies/client.mp4"]},
        "plan": {"title": "Secret title", "analysis": {"transcript": [{"start": 0, "end": 1, "text": "words nobody should paste"}]}},
        "source": {"name": "client.mp4"},
        "error": f"Could not read {home}/Movies/my client/final cut.mp4 with key {key} and Authorization: Bearer abc.def and api_key=xyz",
    }
    text = onboarding.diagnostics(job)
    for leak in (home, "Movies", "client", "final cut", key, "abc.def", "xyz", "Secret title", "A secret client brief", "words nobody should paste"):
        assert leak not in text, leak
    assert "Briefcut " in text and "Python " in text and "FFmpeg " in text
    assert "Last job: status failed" in text and "[path removed]" in text
    assert "Captions:" not in text and "whisper" not in text.lower()


def test_diagnostics_never_copy_doctor_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(onboarding, "doctor_dict", lambda: {"ok": True, "checks": [{"name": "python", "ok": True, "detail": "/Users/someone/.venv/bin/python", "required": True}]})
    assert "someone" not in onboarding.diagnostics(None)


@pytest.mark.parametrize(("text", "gone"), [
    ("failed at /home/ana/My Videos/a b.mp4 now", "My Videos"),
    ("failed at /private/var/folders/x/y/z.mp4", "folders"),
    ("failed at ~/Movies/a.mp4", "Movies"),
    ("token=abcdef123", "abcdef123"),
    ("PASSWORD: hunter2", "hunter2"),
])
def test_scrub(text: str, gone: str) -> None:
    assert gone not in onboarding.scrub(text)


def test_diagnostics_route_is_plain_text_in_json(studio: int) -> None:
    status, body = http_call(studio, "GET", "/api/diagnostics")
    assert status == 200 and "Last job: none" in body["text"]
    assert str(Path.home()) not in body["text"]


# ---- The briefcut command ----


def test_briefcut_with_no_arguments_opens_studio_in_the_default_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import cli
    from vedit.studio import server

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("VEDIT_WORKSPACE", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(settings, "load_checkout_env", lambda: [])  # this checkout may hold real secrets
    seen: list[Path] = []
    monkeypatch.setattr(server, "open_studio", lambda: seen.append(Path.cwd()) or 0)
    assert cli.briefcut_main([]) == 0
    assert seen == [settings.default_workspace().resolve()]


def test_briefcut_with_arguments_is_the_same_tool(capsys: pytest.CaptureFixture[str]) -> None:
    from vedit import __version__, cli

    with pytest.raises(SystemExit) as stopped:
        cli.briefcut_main(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"briefcut {__version__}"


def test_open_studio_joins_one_already_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, studio: int, capsys: pytest.CaptureFixture[str]) -> None:
    from vedit.studio import server

    opened: list[str] = []
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    assert server.open_studio(studio) == 0
    assert opened == [f"http://127.0.0.1:{studio}/"]
    assert "already running" in capsys.readouterr().out


def test_open_studio_takes_a_free_port_when_something_else_holds_the_default(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import socket

    from vedit.studio import server

    def stop(_self: object) -> None:
        raise KeyboardInterrupt

    opened: list[str] = []
    monkeypatch.setattr(server.webbrowser, "open", opened.append)
    monkeypatch.setattr(server._LoopbackServer, "serve_forever", stop)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        assert server.open_studio(port) == 0
    assert len(opened) == 1 and f":{port}/" not in opened[0]
    output = capsys.readouterr().out
    assert "Briefcut is open at" in output and "Briefcut stopped." in output


# ---- scripts/install.sh ----

INSTALL = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"


def install_stubs(folder: Path) -> Path:
    """A uv that records its arguments, and an FFmpeg that has what Briefcut needs."""
    folder.mkdir(parents=True)
    (folder / "uv").write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> {folder}/uv.log
[ "$1 $2" = "tool dir" ] && echo "{folder}"
exit 0
""", encoding="utf-8")
    (folder / "ffmpeg").write_text("#!/bin/sh\nprintf ' .. drawtext x\\n .. subtitles x\\n'\n", encoding="utf-8")
    for name in ("uv", "ffmpeg"):
        (folder / name).chmod(0o755)
    return folder


def run_install(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    stubs = tmp_path / "bin"
    if not stubs.exists():
        install_stubs(stubs)
    environment = {"HOME": str(home), "PATH": f"{stubs}:/usr/bin:/bin", "VEDIT_FFMPEG": str(stubs / "ffmpeg")}
    return subprocess.run(["bash", str(INSTALL), *arguments], env=environment, capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=False)


def test_a_dry_run_shows_the_plan_and_changes_nothing(tmp_path: Path) -> None:
    result = run_install(tmp_path, "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "What is here" in result.stdout and "Dry run" in result.stdout
    assert "tool install" in result.stdout
    assert "https://dev.meta.ai" in result.stdout, "the installer says where the Meta key comes from"
    assert "whisper" not in result.stdout.lower() and "speech model" not in result.stdout.lower()
    assert "whisper" not in INSTALL.read_text(encoding="utf-8").lower()
    assert not list((tmp_path / "home").iterdir()), "a dry run writes nothing to the home folder"
    assert not (tmp_path / "bin" / "uv.log").exists(), "a dry run runs nothing"


def test_install_asks_first_and_does_nothing_without_an_answer(tmp_path: Path) -> None:
    result = run_install(tmp_path)
    assert result.returncode == 0, result.stderr
    assert "--yes" in result.stdout
    assert not (tmp_path / "bin" / "uv.log").exists()


def test_yes_installs_the_checkout_as_a_tool(tmp_path: Path) -> None:
    result = run_install(tmp_path, "--yes")
    assert result.returncode == 0, result.stderr
    log = (tmp_path / "bin" / "uv.log").read_text(encoding="utf-8")
    # The extras matter: without `mcp`, an installed `briefcut mcp` exits asking for the package.
    assert f"tool install --force --reinstall --python 3.13 vedit[mcp,sam] @ {INSTALL.parents[1]}" in log
    assert "briefcut" in result.stdout


def test_install_refuses_an_unknown_option(tmp_path: Path) -> None:
    result = run_install(tmp_path, "--everything")
    assert result.returncode == 2 and "Unknown option" in result.stderr


# ---- The Mac bundle ----


def test_only_a_relocatable_python_is_bundled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from vedit import packaging

    monkeypatch.setattr(packaging.shutil, "which", lambda _name: None)
    monkeypatch.setattr(packaging.sys, "base_prefix", str(tmp_path / "Cellar" / "python@3.13"))
    assert packaging.relocatable_python() is None, "Homebrew's Python is tied to where it was installed"
    managed = tmp_path / "uv" / "python" / "cpython-3.13.1-macos-aarch64-none"
    (managed / "bin").mkdir(parents=True)
    (managed / "bin" / "python3.13").write_text("", encoding="utf-8")
    monkeypatch.setattr(packaging.sys, "base_prefix", str(managed))
    assert packaging.relocatable_python() == managed.resolve()


def test_the_bundle_leaves_out_what_an_editor_never_loads(tmp_path: Path) -> None:
    from vedit import packaging

    source = tmp_path / "cpython"
    for name in ("bin/python3.13", "bin/pip3", "lib/libpython3.13.dylib", "lib/libtcl9.0.dylib", "lib/python3.13/os.py", "lib/python3.13/test/test_os.py",
                 "lib/python3.13/idlelib/idle.py", "lib/python3.13/site-packages/pip/x.py", "lib/python3.13/lib-dynload/_tkinter.cpython-313-darwin.so",
                 "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so", "include/python3.13/Python.h"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    resources = tmp_path / "Resources"
    (resources / "src").mkdir(parents=True)
    packaging._bundle_python(resources, source)
    bundled = {path.relative_to(resources / "python").as_posix() for path in (resources / "python").rglob("*") if path.is_file()}
    assert bundled == {"bin/python3.13", "lib/libpython3.13.dylib", "lib/python3.13/os.py", "lib/python3.13/lib-dynload/_ssl.cpython-313-darwin.so"}
    assert (resources / "src" / "yaml" / "__init__.py").is_file(), "PyYAML travels with the package"
    assert not list((resources / "src" / "yaml").rglob("*.so"))
