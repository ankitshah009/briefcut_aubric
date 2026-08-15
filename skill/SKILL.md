---
name: vedit
description: >
  Local agent-native video editor. Use when the user wants to cut, caption,
  duck music, reframe, or export real footage with FFmpeg and no paid APIs.
  Triggers: edit this video, cut silence, add captions, make shorts, podcast
  edit, duck music, vedit, video_editor.
---

# vedit

Package root: `video_editor/`. CLI: `python -m vedit` or `vedit`.

## Workflow

1. Run `python -m vedit doctor`. Fix required failures before editing.
2. Prefer a YAML brief in the work directory over ad-hoc ffmpeg.
3. `python -m vedit plan brief.yaml` then `python -m vedit render brief.yaml` (or `vedit render DIR` on an existing work dir).
4. Verify `report.json` (duration, size, streams, color). Do not claim success from exit code alone.
5. Hardware encode is auto on Apple Silicon. Pass `--no-hw` for bit-exact software. `web_preview` never uses VideoToolbox.

## One-shots

```bash
python -m vedit do talk.mp4 --cut-silence --captions --music bed.mp3 --preset youtube_1080p
python -m vedit recipe apply shorts --in talk.mp4
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
