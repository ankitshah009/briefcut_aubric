"""Nothing is fixed to one demo, one language, one platform or one default.

Briefcut must work for any footage, any of the languages Meta hears, on any
computer, with the person's own Settings. These tests keep the audit fixes in
place: one language list served from meta.py, "auto" by default, no demo
subjects in examples, "this computer" instead of "this Mac", the locale's own
number format, Ctrl off a Mac, saved values kept, MCP defaults from Settings,
word limits that count scripts without spaces, and agent-safe error messages.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from vedit import meta, settings
from vedit.campaign import CampaignError, CampaignManager

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src" / "vedit" / "studio" / "static"
# Verbatim from dev.meta.ai/docs/speech-to-text#language-biasing.
HEARD_BY_META = (
    "Arabic, Bengali, Dutch, English, French, German, Hebrew, Hindi, Indonesian, Italian, Japanese, Kannada, Korean, "
    "Malay, Mandarin Chinese, Marathi, Polish, Portuguese, Spanish, Tagalog, Tamil, Telugu, Thai, Turkish, and Vietnamese"
)
DEMO_WORDS = re.compile(r"\b(?:oak|bowls?|workshop|wooden|lathe|sanding)\b", re.IGNORECASE)
node = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def _node(script: str, *args: str) -> Any:
    return json.loads(
        subprocess.run(["node", "-e", script, *args], capture_output=True, text=True, check=True, timeout=30).stdout
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("VEDIT_HOME", str(tmp_path / "home"))
    for name in ("MODEL_API_KEY", "VEDIT_META_ENABLED", "VEDIT_META_MODE", "VEDIT_PRIVATE_MODEL"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


# --- Languages: one list, served by the server ----------------------------------------------


def test_one_language_list_names_every_language_and_marks_what_meta_hears() -> None:
    rows = meta.languages()
    assert {row["code"]: row["name"] for row in rows} == meta._LANGUAGE_NAMES
    assert [row["name"] for row in rows] == sorted(row["name"] for row in rows)
    documented = {name.removeprefix("and ").strip() for name in HEARD_BY_META.split(", ")}
    assert len(documented) == 25
    assert {row["name"] for row in rows if row["heard"]} == documented


def test_capabilities_serve_the_list_studio_and_agents_build_from(home: Path) -> None:
    assert meta.capabilities()["languages"] == meta.languages()


def test_the_pages_fallback_picker_is_auto_then_every_language_meta_hears() -> None:
    page = _read("index.html")
    select = re.search(r'<select id="language">(.*?)</select>', page, re.DOTALL).group(1)
    options = [
        (value, html.unescape(text)) for value, text in re.findall(r'<option value="([^"]*)">([^<]*)</option>', select)
    ]
    assert options == [("auto", "Detect automatically")] + [
        (row["code"], row["name"]) for row in meta.languages() if row["heard"]
    ]


def test_studio_settings_and_captions_read_the_served_list() -> None:
    studio, inspector, settings_js = _read("studio.js"), _read("inspector.js"), _read("settings.js")
    assert "fillLanguages(meta.languages);" in studio
    assert 'new Option("Detect automatically", "auto")' in studio
    assert 'req.language || "en"' not in studio and ': "auto";' in studio
    # Caption translation offers every language Muse Spark writes, not only the spoken ones.
    assert "(Kit.caps().meta || {}).languages" in inspector
    assert '[["en", "English"]]' not in settings_js and '[["auto", "Detect automatically"]]' in settings_js


def test_auto_is_the_default_and_never_reaches_voice_transcribe_as_a_hint(home: Path) -> None:
    assert settings.DEFAULT_LANGUAGE == "auto"
    assert settings.default_language() == "auto"
    assert meta._language_bias("auto") == [] and meta._language_bias("") == []
    assert meta._language_bias("es") == ["Spanish"] and meta._language_bias("pt-BR") == ["Portuguese"]
    assert meta._language_bias("kn") == ["Kannada"] and meta._language_bias("tl") == ["Tagalog"]
    # Spark writes Russian, but Voice Transcribe does not list it: no hint, so Meta detects the language.
    assert meta._language_bias("ru") == [] and meta._language_bias("xx") == []


# --- No demo, no Mac-only words, the person's own locale ------------------------------------


def test_no_studio_example_names_the_demo_footage() -> None:
    found = {
        path.name: sorted({m.group(0) for m in DEMO_WORDS.finditer(path.read_text(encoding="utf-8"))})
        for path in sorted(STATIC.iterdir())
        if path.suffix in {".html", ".js", ".css"}
    }
    assert {name: hits for name, hits in found.items() if hits} == {}
    inspector, page = _read("inspector.js"), _read("index.html")
    assert (
        'placeholder: "The person in the red jacket"' in inspector
        and 'placeholder: "A soft studio backdrop"' in inspector
    )
    assert 'placeholder="Hold the first shot longer, then cut to the close-up."' in page
    server = (ROOT / "src" / "vedit" / "mcp_server.py").read_text(encoding="utf-8")
    assert not DEMO_WORDS.search(server) and '"subject": "the person"' in server


def test_studio_says_this_computer_and_uses_the_locale() -> None:
    for path in sorted(STATIC.iterdir()):
        if path.suffix in {".html", ".js", ".css"}:
            text = path.read_text(encoding="utf-8")
            assert "this Mac" not in text, path.name
            assert 'NumberFormat("en-US")' not in text, path.name
    assert "new Intl.NumberFormat()" in _read("meta-calls.js")


@pytest.mark.parametrize(
    ("platform", "said"), [("darwin", "this Mac"), ("linux", "this computer"), ("win32", "this PC")]
)
def test_private_mode_names_the_machine_it_runs_on(
    platform: str, said: str, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    assert meta.this_computer() == said
    assert meta._private_status()["runs_on"] == said + " (llama.cpp)"


def test_shortcuts_name_the_modifier_this_keyboard_has() -> None:
    page = _read("index.html")
    assert "<kbd>Cmd</kbd>" not in page and page.count("<kbd data-mod>Cmd</kbd>") == 3
    assert page.index('<script src="platform.js">') < page.index('<script src="timeline.js">')
    timeline = _read("timeline.js")
    assert "Cmd+Z" not in timeline and 'window.BriefcutPlatform.mod) || "Ctrl"' in timeline


@node
def test_platform_detection_says_cmd_only_on_apple_keyboards() -> None:
    script = r"""
const vm = require("vm"); const fs = require("fs");
const source = fs.readFileSync(process.argv[1], "utf8");
const navs = [{ userAgentData: { platform: "macOS" } }, { platform: "MacIntel" }, { platform: "Win32" }, { platform: "Linux x86_64" },
              { userAgentData: { platform: "Windows" }, platform: "MacIntel" }, { userAgent: "Mozilla/5.0 (X11; Linux x86_64)" }];
process.stdout.write(JSON.stringify(navs.map((navigator) => { const window = { navigator }; vm.runInNewContext(source, { window }); return window.BriefcutPlatform.mod; })));
"""
    assert _node(script, str(STATIC / "platform.js")) == ["Cmd", "Cmd", "Ctrl", "Ctrl", "Ctrl", "Ctrl"]


def test_saved_lengths_budgets_and_languages_are_kept_not_dropped() -> None:
    studio = _read("studio.js")
    assert "[15, 30, 60, 90].includes" not in studio and "[0.25, 0.5, 1, 2].includes" not in studio
    assert 'chooseOption($("target-duration"), length' in studio and 'chooseOption($("budget"), budget' in studio
    assert 'chooseOption($("language"), language' in studio


def test_export_offers_match_source_when_the_catalogue_has_it() -> None:
    inspector = _read("inspector.js")
    assert 'function fpsLabel(value) { return value === "source" ? "Match source" : value + " fps"; }' in inspector
    assert "label: fpsLabel(value)" in inspector


# --- The story panel says what actually planned the story -----------------------------------


@node
def test_story_description_follows_the_plan_provider_and_transcriber() -> None:
    studio = _read("studio.js")
    source = re.search(r"  function storyDescription\(plan, request\) \{.*?\n  \}\n", studio, re.DOTALL).group(0)
    cases = [
        [
            {
                "provider": "meta",
                "summary": "Opens on the reveal.",
                "analysis": {
                    "method": "meta",
                    "transcribed": True,
                    "transcribed_by": "meta-voice-transcribe",
                    "sentences": [{"start": 0, "end": 2, "text": "Hi."}],
                },
            },
            {"provider": "meta"},
        ],
        [{"provider": "meta", "summary": "", "analysis": {"method": "meta", "sentences": []}}, {"provider": "meta"}],
        [
            {
                "provider": "local",
                "summary": "Local analysis found 3 scene cuts. This is deterministic signal analysis; local mode does not choose a story with AI.",
            },
            {"provider": "local"},
        ],
        [
            {
                "provider": "meta",
                "summary": "A video made from your script: 2 scenes.",
                "analysis": {"method": "script"},
            },
            {"provider": "meta"},
        ],
    ]
    said = _node(
        source
        + "process.stdout.write(JSON.stringify("
        + json.dumps(cases)
        + ".map(([plan, request]) => storyDescription(plan, request))));"
    )
    assert said[0] == (
        "Muse Spark planned this story from the footage and the Meta Voice Transcribe transcript. "
        "Each cut ends on a sentence boundary. Opens on the reveal."
    )
    assert said[1] == "Muse Spark planned this story from the footage. Review every moment before you make the video."
    assert (
        said[2]
        == "Moments were picked from scene cuts and pauses in the footage. Review every moment before you make the video."
    )
    assert said[3] == "A video made from your script: 2 scenes."


# --- Meta calls panel: why a call failed, and whether it cost anything -----------------------


def test_failed_meta_calls_say_why_and_released_ones_cost_nothing() -> None:
    js, css = _read("meta-calls.js"), _read("meta-calls.css")
    assert 'const RELEASED = "Meta refused it before running it, so nothing was billed.";' in js
    assert "call.released === true" in js and "chip.title = reason || hint;" in js
    assert 'el("p", "mc-reason"' in js and ".mc-reason {" in css
    # A released call is not shown as a reserved cost.
    assert 'const reserved = !local && !released && call.cost_kind !== "actual";' in js


# --- Words in any script --------------------------------------------------------------------


def test_a_script_without_spaces_meets_the_word_limit(tmp_path: Path, home: Path) -> None:
    manager = CampaignManager(tmp_path / "ws", start_worker=False)
    try:
        with pytest.raises(CampaignError, match="Keep the script to"):
            manager.create_script({"script": "木の器を作る工程を見せる。" * 80})
        with pytest.raises(CampaignError, match="needs Meta"):
            manager.create_script({"script": "木の器を作る工程を見せる。" * 10})
    finally:
        manager.close()


# --- MCP: Settings defaults, and errors an agent can act on ---------------------------------

mcp = pytest.importorskip("mcp")
from mcp.client.client import Client  # noqa: E402

from vedit import mcp_server  # noqa: E402


def _payload(result: Any) -> Any:
    text = "".join(item.text for item in result.content if getattr(item, "type", "") == "text")
    failed = getattr(result, "is_error", None)
    if failed is None:
        failed = getattr(result, "isError", False)
    return {"tool_error": text} if failed else json.loads(text)


@pytest.fixture
def stub(home: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("VEDIT_WORKSPACE", str(home / "ws"))
    sent: list[tuple[str, Any]] = []

    class Stub:
        _closed = False

        def create(self, value: dict[str, Any]) -> dict[str, Any]:
            sent.append(("story", value))
            return {"id": "story-1", "status": "queued"}

        def create_script(self, value: dict[str, Any]) -> dict[str, Any]:
            sent.append(("script", value))
            return {"id": "script-1", "status": "queued"}

        def find(self, job_id: str, value: dict[str, Any]) -> dict[str, Any]:
            raise NameError("name '_health' is not defined")

        def get(self, job_id: str) -> dict[str, Any]:
            raise RuntimeError(
                "Meta said: Authorization: Bearer sk-live-abc123def456ghi789 failed, api_key=hunter2hunter2 and token: zzz"
            )

    monkeypatch.setattr(mcp_server, "_manager", Stub)
    yield sent
    mcp_server.shutdown()


def test_create_tools_leave_language_and_budget_to_settings(stub: list[tuple[str, Any]]) -> None:
    async def flow() -> None:
        async with Client(mcp_server.build_server()) as client:
            await client.call_tool("briefcut_create_story", {"paths": ["/x.mp4"], "goal": "Show it"})
            await client.call_tool("briefcut_script_to_video", {"script": "One line."})
            await client.call_tool(
                "briefcut_create_story", {"paths": ["/x.mp4"], "goal": "Show it", "language": "ja", "budget_usd": 1.5}
            )

    asyncio.run(flow())
    (_, story), (_, script), (_, chosen) = stub
    for value in (story, script):
        assert "language" not in value and "budget_usd" not in value
    assert chosen["language"] == "ja" and chosen["budget_usd"] == 1.5


def test_status_carries_the_language_list_and_the_last_problem(stub: list[tuple[str, Any]]) -> None:
    async def flow() -> dict[str, Any]:
        async with Client(mcp_server.build_server()) as client:
            return _payload(await client.call_tool("briefcut_status", {}))

    status = asyncio.run(flow())
    assert status["meta"]["languages"] == meta.languages()
    assert "last_problem" in status["meta"]


def test_an_unexpected_error_is_specific_and_carries_no_secret(
    stub: list[tuple[str, Any]], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "LLM|meta-key-value-0123456789")

    def broken() -> dict[str, Any]:
        raise KeyError("render: key LLM|meta-key-value-0123456789 was not found")

    async def flow() -> list[Any]:
        async with Client(mcp_server.build_server()) as client:
            found = _payload(
                await client.call_tool("briefcut_find_moments", {"job_id": "a" * 32, "query": "the person"})
            )
            got = _payload(await client.call_tool("briefcut_get", {"job_id": "a" * 32}))
            monkeypatch.setattr(mcp_server, "capabilities", broken)
            status = _payload(await client.call_tool("briefcut_status", {}))
            return [found, got, status]

    found, got, status = asyncio.run(flow())
    assert "NameError" in found["tool_error"] and "_health" in found["tool_error"] and "find" in found["tool_error"]
    assert "RuntimeError" in got["tool_error"]
    assert "KeyError" in status["tool_error"]
    log = capsys.readouterr().err
    assert "Traceback" in log and "NameError" in log
    for text in (found["tool_error"], got["tool_error"], status["tool_error"], log):
        for secret in ("sk-live-abc123def456ghi789", "hunter2hunter2", "meta-key-value-0123456789", "zzz"):
            assert secret not in text
        assert "Traceback" not in text or text is log
