# Connection and recovery

Read this when the `briefcut_*` tools are missing, the server will not start,
or a call fails before it reaches Briefcut. Diagnose the error you actually
see. Do not repeat every setup step for every symptom.

## How each install starts the server

| Install | What runs |
| --- | --- |
| Claude Code plugin | `sh ${CLAUDE_PLUGIN_ROOT}/.claude-plugin/mcp-launcher.sh`. It finds uv, installs the runtime packages (the `mcp` and `sam` extras, no development tools) into the plugin's own `.venv`, then runs `vedit mcp`. |
| Repository `.mcp.json` | `uv run --no-dev --extra mcp --extra sam vedit mcp`, started in the repository root. |
| `claude mcp add` by hand | Whatever command was registered, usually `uv run --directory /path/to/briefcut vedit mcp`. |
| `scripts/install.sh` | Installs the `briefcut` and `vedit` commands with the `mcp` and `sam` extras. Register `briefcut mcp` by its absolute path. |

The first plugin start downloads Python packages, and CPython 3.13 when it is
missing. It needs network access once. Later starts reuse the `.venv`.

Use the plugin or a manual registration, not both. Two `briefcut` servers on
the same workspace contend for one lease, and the second gets a lease error.

## Symptoms

| You see | Cause | Do this |
| --- | --- | --- |
| No `briefcut_*` tools, and `/mcp` shows briefcut as failed | The server did not start | Read the server's error in `/mcp`. The cases below cover the common ones. |
| "uv was not found" | uv is not installed, or not on the client's PATH | Ask the user to install uv (`brew install uv`, or the installer at https://docs.astral.sh/uv/). Then reconnect in `/mcp`. |
| "could not install its Python packages" | The first start had no network, or the download failed | Fix the error printed above it, then reconnect. |
| "the MCP server needs the `mcp` package" | An install without the `mcp` extra | Rerun `bash scripts/install.sh`, or `uv tool install --force "vedit[mcp,sam] @ git+https://github.com/ankitshah009/briefcut_aubric"`. |
| "Another Studio is already processing this workspace" | Studio, the Mac app, or another agent holds the workspace lease | Tell the user. Close the other one, or restart this server with a different `VEDIT_WORKSPACE`. |
| A tool error that names FFmpeg or ffprobe | FFmpeg is missing or lacks the needed filters | Run `vedit doctor` (from a checkout, `uv run vedit doctor`). On a Mac, `brew install ffmpeg-full`. On Debian or Ubuntu, `apt-get install ffmpeg`. `VEDIT_FFMPEG` can point at another build. |
| "Meta is not enabled", or `briefcut_status` shows `ready: false` with a Meta line in `missing` | No Meta API key, or Meta calls are switched off | Briefcut cannot work without Meta. Tell the user to create a key at https://dev.meta.ai and paste it in Briefcut Settings, or set `MODEL_API_KEY` with `VEDIT_META_ENABLED=1`. Do not change keys or settings yourself. |
| "Briefcut runs on Meta models only, so there is no local mode" | A call passed `provider="local"` or `caption_provider="local"` | Leave both out. They default to `meta`. |
| "The edit queue is full" | Too many jobs at once | Wait for a job to finish, then try again. |
| `briefcut_wait` timed out | The job is still running | Call `briefcut_wait` again. Do not start a second job. |

## Where projects go

The server uses `VEDIT_WORKSPACE` when it is set. Otherwise it uses the folder
it started in, which for Claude Code is usually the project you opened. Files
go in `vedit-sessions/` there. Starting the server writes nothing. The folder
appears on the first tool call that needs it.

In a git repository, add `vedit-sessions/` to `.gitignore` so footage is not
committed. To keep every project in one place, register the server by hand
with `-e VEDIT_WORKSPACE=/path/to/folder`.

To share projects with Studio or the Mac app, point `VEDIT_WORKSPACE` at the
same folder. Only one of them can work on it at a time.

## When MCP stays unavailable

Use the CLI from a checkout (`uv run vedit ...`) or an installed `vedit`. See
[CLI and briefs](cli-and-briefs.md). The proof rule is the same: read
`report.json` and probe the file.
