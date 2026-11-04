"""Agent packaging: the Claude Code plugin, the project .mcp.json, the skill, and install.sh.

The stdio tests start the MCP server with the exact command each config
declares and speak raw JSON-RPC to it, so they do not depend on the mcp
client library's API, which changed between major versions.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / ".claude-plugin" / "plugin.json"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"
PROJECT_MCP = ROOT / ".mcp.json"
SKILL_DIR = ROOT / "skills" / "briefcut"
SKILL = SKILL_DIR / "SKILL.md"
INSTALL = ROOT / "scripts" / "install.sh"
PLUGIN_ROOT_VAR = "${CLAUDE_PLUGIN_ROOT}"
REPOSITORY = "ankitshah009/briefcut_aubric"

# The tools every Briefcut MCP server has had since `vedit mcp` shipped.
CORE_TOOLS = {
    "briefcut_import_clip", "briefcut_create_story", "briefcut_find_moments", "briefcut_make_shorts",
    "briefcut_script_to_video", "briefcut_get", "briefcut_list", "briefcut_wait", "briefcut_render",
    "briefcut_revise", "briefcut_receipts", "briefcut_artifact_path", "briefcut_cancel", "briefcut_retry",
}
# One server start can install packages into a fresh .venv; later starts take about a second.
FIRST_START_SECONDS = 300
REPLY_SECONDS = 60


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n(.*)\Z", text, re.DOTALL)
    assert match, f"{path} must start with YAML frontmatter"
    return yaml.safe_load(match.group(1)), match.group(2)


def _project_extras() -> list[str]:
    return list(_pyproject()["project"]["optional-dependencies"])


def _extras(args: list[str]) -> list[str]:
    return [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--extra"]


def _plugin_command(plugin_root: Path) -> list[str]:
    server = _json(PLUGIN)["mcpServers"]["briefcut"]
    return [part.replace(PLUGIN_ROOT_VAR, str(plugin_root)) for part in [server["command"], *server["args"]]]


def _project_command() -> list[str]:
    server = _json(PROJECT_MCP)["mcpServers"]["briefcut"]
    return [server["command"], *server["args"]]


# --- manifests ------------------------------------------------------------------


def test_plugin_and_marketplace_agree_with_the_package():
    plugin, market, project = _json(PLUGIN), _json(MARKETPLACE), _pyproject()
    version = project["project"]["version"]
    assert plugin["name"] == "briefcut"
    assert plugin["version"] == version, "bump .claude-plugin/plugin.json with pyproject.toml"
    assert plugin["license"] == project["project"]["license"]["text"]
    assert REPOSITORY in plugin["repository"]

    assert market["name"] == "briefcut"
    [entry] = [row for row in market["plugins"] if row["name"] == plugin["name"]]
    assert entry["version"] == version, "bump .claude-plugin/marketplace.json with pyproject.toml"
    source = (MARKETPLACE.parent.parent / entry["source"]).resolve()
    assert (source / ".claude-plugin" / "plugin.json").resolve() == PLUGIN.resolve()

    skills = (ROOT / plugin["skills"]).resolve()
    assert (skills / "briefcut" / "SKILL.md").is_file()


def test_plugin_server_runs_the_launcher_with_runtime_extras():
    server = _json(PLUGIN)["mcpServers"]["briefcut"]
    assert shutil.which(server["command"]), f"{server['command']} is not on PATH"
    assert any(PLUGIN_ROOT_VAR in arg for arg in server["args"]), "plugin paths must use ${CLAUDE_PLUGIN_ROOT}"
    launcher = Path(_plugin_command(ROOT)[1])
    assert launcher.is_file()
    subprocess.run(["sh", "-n", str(launcher)], check=True)

    text = launcher.read_text(encoding="utf-8")
    extras = set(_project_extras())
    for needed in ("mcp", "sam"):
        assert needed in extras
        assert f"--extra {needed}" in text
    assert "--no-dev" in text, "the plugin must not install pytest and other development tools"
    assert re.search(r'exec "\$uv_bin" run --project "\$root" --no-sync vedit mcp$', text, re.MULTILINE)
    # stdout is the MCP channel: every message the launcher prints goes to stderr.
    for line in text.splitlines():
        if line.strip().startswith("echo "):
            assert line.rstrip().endswith(">&2"), line


def test_project_mcp_json_runs_vedit_mcp_with_runtime_extras():
    command = _project_command()
    assert command[0] == "uv"
    assert command[1] == "run"
    assert command[-2:] == ["vedit", "mcp"]
    assert "--no-dev" in command
    assert set(_extras(command)) == {"mcp", "sam"} <= set(_project_extras())
    assert _pyproject()["project"]["scripts"]["vedit"] == "vedit.cli:main"

    from vedit.cli import _parser, cmd_mcp

    assert _parser().parse_args(["mcp"]).func is cmd_mcp


def test_both_configs_name_the_same_server():
    assert set(_json(PLUGIN)["mcpServers"]) == set(_json(PROJECT_MCP)["mcpServers"]) == {"briefcut"}


# --- skill ----------------------------------------------------------------------


def test_skill_frontmatter_and_links():
    meta, body = _frontmatter(SKILL)
    assert meta["name"] == SKILL_DIR.name == "briefcut"
    description = meta["description"]
    assert isinstance(description, str) and 0 < len(description) <= 1024
    assert "Do not use" in description, "say when not to use the skill"
    assert str(meta["metadata"]["version"]).strip()
    assert len(SKILL.read_text(encoding="utf-8").splitlines()) <= 200, "move detail to references/"

    for page in [SKILL, *sorted((SKILL_DIR / "references").glob("*.md"))]:
        text = page.read_text(encoding="utf-8")
        assert "—" not in text, f"{page.name}: no em dashes"
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", text):
            if "://" not in target:
                assert (page.parent / target).exists(), f"{page.name} links to missing {target}"
    for rule in ("timeline.otio.json", "passed == total", "consent", "VEDIT_WORKSPACE"):
        assert rule in body


def test_old_skill_path_points_at_the_new_one():
    text = (ROOT / "skill" / "SKILL.md").read_text(encoding="utf-8")
    assert not text.startswith("---"), "a second frontmatter would load a second, older skill"
    assert "../skills/briefcut/SKILL.md" in text


def test_readme_documents_the_plugin_install():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    market = _json(MARKETPLACE)
    assert f"/plugin marketplace add {REPOSITORY}" in readme
    assert f"/plugin install briefcut@{market['name']}" in readme


# --- install.sh -----------------------------------------------------------------


def test_install_script_installs_the_mcp_extra():
    text = INSTALL.read_text(encoding="utf-8")
    assert re.search(r'^requirement="vedit\[mcp,sam\] @ \$source_spec"$', text, re.MULTILINE)
    assert 'tool install --force --reinstall --python 3.13 "$requirement"' in text
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is not installed")
    subprocess.run([bash, "-n", str(INSTALL)], check=True)
    plan = subprocess.run([bash, str(INSTALL), "--dry-run"], capture_output=True, text=True, timeout=60, check=True).stdout
    assert f"'vedit[mcp,sam] @ {ROOT}'" in plan, plan
    assert "briefcut\" mcp" in plan


# --- the server over stdio --------------------------------------------------------


class _Stdio:
    """A JSON-RPC peer on a child's stdin and stdout, newline-delimited as MCP's stdio transport is."""

    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str]):
        self.proc = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, encoding="utf-8")
        self.lines: queue.Queue[str | None] = queue.Queue()
        self.stderr: list[str] = []
        threading.Thread(target=self._read_stdout, daemon=True).start()
        self._stderr_reader = threading.Thread(target=lambda: self.stderr.extend(self.proc.stderr), daemon=True)
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def stderr_tail(self) -> str:
        if self.proc.poll() is not None:
            self._stderr_reader.join(timeout=5)
        return "".join(self.stderr)[-2000:]

    def send(self, message: dict[str, Any]) -> None:
        try:
            self.proc.stdin.write(json.dumps(message) + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            self.proc.wait(timeout=30)
            pytest.fail(f"the server exited (status {self.proc.returncode}) before it read a request. stderr:\n{self.stderr_tail()}")

    def reply(self, ident: int, timeout: float) -> dict[str, Any]:
        while True:
            try:
                line = self.lines.get(timeout=timeout)
            except queue.Empty:
                pytest.fail(f"no reply to request {ident} in {timeout:g}s. stderr:\n{self.stderr_tail()}")
            if line is None:
                self.proc.wait(timeout=30)
                pytest.fail(f"the server exited (status {self.proc.returncode}) before replying to {ident}. stderr:\n{self.stderr_tail()}")
            message = json.loads(line)  # anything else on stdout would corrupt the MCP channel
            if message.get("id") == ident:
                assert "error" not in message, message
                return message["result"]

    def close(self, timeout: float = 30) -> int:
        self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=timeout)
        finally:
            if self.proc.poll() is None:
                self.proc.kill()
                self.proc.wait()


def _client_env(workspace: Path) -> dict[str, str]:
    # A client starts the server with its own environment: no virtualenv of ours, and no Meta switch.
    env = {key: value for key, value in os.environ.items() if key not in {"VIRTUAL_ENV", "MODEL_API_KEY", "VEDIT_META_ENABLED", "VEDIT_WORKSPACE"}}
    env["VEDIT_WORKSPACE"] = str(workspace)
    return env


def _skill_tools() -> set[str]:
    text = SKILL.read_text(encoding="utf-8")
    return set(re.findall(r"`(briefcut_[a-z_]+)", text))


@pytest.mark.parametrize("config", ["project", "plugin"])
def test_configured_command_serves_briefcut_over_stdio(config: str, tmp_path: Path):
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed")
    workspace = tmp_path / "workspace"
    if config == "project":
        # Claude Code starts project servers in the repository root.
        command, cwd = _project_command(), ROOT
    else:
        # A plugin server starts wherever the agent runs, far from the plugin's own folder.
        cwd = tmp_path / "agent-project"
        cwd.mkdir()
        command = _plugin_command(ROOT)

    peer = _Stdio(command, cwd=cwd, env=_client_env(workspace))
    try:
        peer.send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                   "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "packaging-test", "version": "0"}}})
        init = peer.reply(1, FIRST_START_SECONDS)
        assert init["serverInfo"]["name"] == "briefcut"
        assert init["serverInfo"]["version"] == _pyproject()["project"]["version"]
        assert "tools" in init["capabilities"]

        peer.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        peer.send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = {tool["name"] for tool in peer.reply(2, REPLY_SECONDS)["tools"]}
        assert CORE_TOOLS <= names, f"missing {sorted(CORE_TOOLS - names)}"
        assert _skill_tools() <= names, f"the skill names tools the server lacks: {sorted(_skill_tools() - names)}"

        # Starting the server writes nothing; the first manager call claims the workspace.
        assert not (workspace / "vedit-sessions").exists()
        peer.send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "briefcut_list", "arguments": {}}})
        listed = peer.reply(3, REPLY_SECONDS)
        assert listed.get("isError") is not True, listed
        assert json.loads(listed["content"][0]["text"]) == {"jobs": []}
        assert (workspace / "vedit-sessions" / "campaigns").is_dir()
    finally:
        code = peer.close()
    assert code == 0, peer.stderr_tail()
    if config == "plugin":
        assert list(cwd.iterdir()) == [], "with VEDIT_WORKSPACE set, nothing belongs in the agent's folder"
