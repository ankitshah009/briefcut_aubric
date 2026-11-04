# Muse Code drives Briefcut

Recorded 2026-11-04 at 18:33 UTC on an Apple M5 Max.

- Agent: Muse Code 1.4.14 (1.4.14-R7309.1), headless `muse exec`.
- Model: `muse-spark-1.3-contributor`, the account default at the time, through the Meta Model API.
- MCP server: Briefcut's `vedit mcp`, started by Muse Code over stdio from `~/.config/muse/settings.json`, with a scratch workspace.
- Tools offered: only Briefcut's, through `-c 'run.toolset=["mcp__briefcut__*"]'`.

Command:

```bash
muse exec --trust-workspace --approval-mode never --contributor-mode off --reasoning-effort low \
  -c 'run.toolset=["mcp__briefcut__*"]' --json \
  "Use only the briefcut MCP tools. Import the clip .../examples/demo/workshop.mp4 with briefcut_import_clip, then reply with one line giving its duration in seconds and its width x height. Do not create a story or render anything."
```

What happened, from `20261104T183316Z-import-clip.jsonl`:

1. Muse Spark chose `mcp__briefcut__briefcut_import_clip`. Muse Code recorded the call as `allow:policy`.
2. Briefcut copied the clip into its inbox and probed it: 5,357,476 bytes, 40.02 s, 1280x720, with audio.
3. Muse Code answered: "Duration 40.02s, 1280x720."

Usage reported by Muse Code: 2 model calls, 45,631 input tokens (22,641 cached), 160 output tokens, 34 reasoning tokens. At contributor prices ($0.10 in and $0.20 out per million tokens) that is under $0.01.

The run log still names the contributor model, although the command passed `--contributor-mode off`. Choose the model with `--model` to be sure. The clip is the procedural demo clip, not real footage.
