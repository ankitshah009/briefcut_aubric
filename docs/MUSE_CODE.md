# Briefcut in Muse Code

Muse Code is Meta's coding agent. It can drive Briefcut through Briefcut's MCP server, so Meta's agent edits video with Meta's models. Briefcut still runs on your computer, and every model it calls is a Meta model.

## 1. Add the server

Muse Code reads MCP servers from `~/.config/muse/settings.json`. Add a `briefcut` entry under `mcpServers`. Keep `"schema_version": 1` and anything already in the file. Replace `/path/to/briefcut_aubric` with your checkout. Meta's docs spell the key `mcp_servers`; Muse Code 1.4.14 accepts that and saves it back as `mcpServers`.

```json
{
  "schema_version": 1,
  "mcpServers": {
    "briefcut": {
      "transport": "stdio",
      "command": "uv",
      "args": ["run", "--project", "/path/to/briefcut_aubric", "--no-dev", "--extra", "mcp", "--extra", "sam", "vedit", "mcp"],
      "env": {"VEDIT_WORKSPACE": "${HOME}/Library/Application Support/Vedit"},
      "mode": "optional"
    }
  }
}
```

`VEDIT_WORKSPACE` here is Studio's project folder on a Mac, so projects that Muse Code makes open in Studio too. One process holds a workspace at a time, so close Studio while Muse Code is editing, then open Studio to see the result.

The server takes the Meta key from Briefcut's own settings or from `MODEL_API_KEY` in the environment you start Muse Code from. Never put the key in `settings.json`.

## 2. The skill

The repository ships the Briefcut skill at `.agents/skills/briefcut/`, which is where Muse Code looks for project skills. It links to `skills/briefcut/`, the same skill the Claude Code plugin uses. Muse Code loads project skills only in a trusted workspace. Trust the checkout once, or pass `--trust-workspace` for a single run.

## 3. Try it

From the checkout:

```bash
muse exec --trust-workspace --model muse-spark-1.3 \
  -c 'run.toolset=["mcp__briefcut__*"]' \
  "Use the briefcut tools. Import /path/to/clip.mp4, make a 20 second 9:16 story about it, render it, and show me the receipts."
```

`run.toolset` limits the run to Briefcut's tools. In an interactive `muse` session, `/mcp` lists the connected servers and their tools.

`muse-spark-1.3-contributor` is cheaper, and Meta may train on what it receives. That includes Briefcut's tool results, such as plans and caption text. Pick the standard model for footage you would not share.

## Evidence

`docs/evidence/muse-code/` holds a recorded headless run: Muse Code 1.4.14 called `briefcut_import_clip` on the demo clip and reported its duration and size.
