---
name: vedit
description: >
  Local agent-native video editor. Use when the user wants to cut, caption,
  duck music, reframe, or export real footage with FFmpeg and no paid APIs.
  Triggers: edit this video, cut silence, add captions, make shorts, podcast
  edit, duck music, vedit, video_editor, agent, studio, package dmg.
---

# vedit

Package root: `briefcut/`. Environment: uv only (`uv sync` creates `.venv` on CPython 3.13 from `uv.lock`). CLI: `uv run vedit` (equivalently `.venv/bin/python -m vedit`).

## Workflow

1. Run `uv run vedit doctor`. Fix required failures before editing.
2. Prefer a YAML brief in the work directory over ad-hoc ffmpeg.
3. `uv run vedit plan brief.yaml` then `uv run vedit render brief.yaml` (or `vedit render DIR` on an existing work dir).
4. Verify `report.json` (duration, size, streams, color). Do not claim success from exit code alone.
5. Hardware encode is auto on Apple Silicon. Pass `--no-hw` for bit-exact software. `web_preview` never uses VideoToolbox.

## Agent

The editorial agent does not call an LLM. The English parser is deterministic. No paid caption or enhance APIs. The brief stays the source of truth. `report.json` is the proof.

```bash
uv run vedit agent "cut silence, reframe 9:16, burn captions" talk.mp4
uv run vedit agent "cut silence, reframe 9:16, burn captions" talk.mp4 --plan --work-dir DIR
```

Flags: `--plan`, `--no-analyze`, `--revise N`, `--work-dir DIR`.

Session files: `intent.json`, `inventory.json`, `decisions.md`, `brief.yaml`, `work/project.json`, `work/render.sh`. Render is the default and probes the output. Critique may revise the brief once into `rev1/`. Exit 0 planned or verified. Exit 3 failed critique.

Loop: inventory, intent, decide, compile, execute, critique, at most one revise.

```bash
uv run vedit tools
uv run vedit studio
uv run vedit package dmg --out dist/Briefcut.dmg
```

`vedit tools` is stdio JSON: `doctor`, `inventory`, `parse_intent`, `decide`, `plan`, `render`, `critique`, `revise`. An external agent drives it.

`vedit studio` binds 127.0.0.1:8765 only. No account, no cloud. `package dmg` stages `dist/Briefcut.app` and builds a UDZO disk image. The app needs Python 3.13+ and ffmpeg already on the machine. Bundle id `ai.aubric.briefcut`. The visible app name is Briefcut. The engine, CLI, and Python package stay `vedit`.

## One-shots

```bash
uv run vedit do talk.mp4 --cut-silence --captions --music bed.mp3 --preset youtube_1080p
uv run vedit recipe apply shorts --in talk.mp4
```

## Brief skeleton

```yaml
name: edit
sources: { main: ./talk.mp4 }
ops:
  - { op: cut_silence }
  - { op: captions, burn: true }
  - { op: export, presets: [youtube_1080p] }
```

Ops live in `src/vedit/compile.py` (`OPS`). Add new ops there, not as raw ffmpeg in the brief.

Set `VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` on this Mac so titles and burned captions work.
