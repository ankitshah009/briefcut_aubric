# AGENTS.md

This package is the NLE. You do not write FFmpeg by hand.

## Do the job

1. `uv run vedit doctor` — if required checks fail, stop and say what to install. The environment is uv-managed (`uv sync` creates `.venv`); never use bare `venv`/`pip`.
2. Write a brief (`*.yaml`) or call `vedit do ...` / `Edit(...)`.
3. `vedit plan <brief> --work-dir DIR` and read `project.json` / `render.sh` before a long render.
4. `vedit execute DIR` or `vedit render DIR` to run that program. `--resume` skips finished steps.
5. Read `report.json`. Confirm duration, width/height, has_audio, has_video, color. That is the proof, not “the command ran.”
6. `timeline.otio.json` is interchange, not the program. Do not hand-edit it and expect a re-render.

## Brief rules

- Every media file is a named entry under `sources`. Ops refer to those names.
- Ops run in order on a tape (current clip). `concat` / `slideshow` create the tape.
- Always end with `export`. Pick a preset: `youtube_1080p`, `shorts_9x16`, `podcast_audio`, `web_preview`.
- Relative paths resolve against the brief file.

## Never

- Call paid caption / enhance / “AI editor” APIs.
- Invent ffmpeg flags. If an op is missing, add it to `src/vedit/compile.py` `OPS` and a test.
- Claim a render worked without probing the output file.

## Env

```text
VEDIT_FFMPEG          absolute ffmpeg (prefer ffmpeg-full)
VEDIT_FFPROBE         optional
VEDIT_WHISPER_MODEL   ggml .bin for whisper-cli
VEDIT_FONT            .ttf / .otf
VEDIT_FASTER_WHISPER_MODEL   if using faster-whisper (default base.en)
```
