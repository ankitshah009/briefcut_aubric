# CLI and briefs

The CLI edits with a brief: a YAML program that compiles to FFmpeg steps.
Use it for brief-level work the MCP tools do not cover, such as silence cuts,
music ducking, loudness, and export presets. From a checkout, prefix every
command with `uv run`. An installed copy provides `vedit` and `briefcut`
(the same tool).

## Check the machine once

```bash
uv run vedit doctor
```

Two checks are required: FFmpeg, and a Meta API key (`meta`). If one fails,
stop and tell the user what is missing. `vedit setup` prints the exact
commands for this machine and where to create a key (https://dev.meta.ai).
Captions come from Meta Voice Transcribe, so a brief with `captions` needs the
key too. Nothing is downloaded: Briefcut runs no local model.

## One shot

```bash
vedit do talk.mp4 --cut-silence --captions --music bed.mp3 --preset youtube_1080p
vedit do "cut silence from talk.mp4; captions; duck bed.mp3; reframe 9:16; export shorts"
```

Add `--plan --work-dir DIR` to write the program without rendering.

## Brief, plan, render

```yaml
name: edit
sources:
  main: ./talk.mp4
  music: ./bed.mp3
ops:
  - { op: cut_silence }
  - { op: captions, burn: true }
  - { op: music, source: music, duck: true, volume_db: -18 }
  - { op: loudnorm, i: -16 }
  - { op: export, presets: [shorts_9x16] }
```

- Every media file is a named entry under `sources`. Ops refer to those names.
- Ops run in order on the current clip. `concat` and `slideshow` create it.
- Always end with `export`. Common presets: `youtube_1080p`, `shorts_9x16`,
  `podcast_audio`, `web_preview`. `vedit presets` lists all of them.
- Relative paths resolve against the brief file.
- The ops are listed in the README and live in `src/vedit/compile.py` `OPS`.

```bash
vedit plan brief.yaml --work-dir DIR     # writes project.json, render.sh, steps/
vedit execute DIR                        # runs that program, no recompile
vedit execute DIR --resume               # skips steps whose outputs exist
```

Read `project.json` and `render.sh` before a long render.

## Proof

Each work dir gets a `report.json`. Confirm duration, width and height,
`has_audio`, `has_video`, and color before you report success. Probe any file
with `vedit probe FILE`, which prints JSON.

`timeline.otio.json` is interchange for other editors. It is not the program.
Editing it changes nothing on the next render.

## Other commands

| Command | Does |
| --- | --- |
| `vedit agent "goal" clip.mp4 --plan --work-dir DIR` | Writes an editorial session (intent, decisions, brief) from a goal. Without `--plan` it renders and probes. Exit 0 means planned or verified, exit 3 a failed critique. |
| `vedit recipe list` and `vedit recipe apply shorts --in talk.mp4` | Named end-to-end jobs. |
| `vedit meta status` | Says whether Meta is configured and enabled. It sends nothing. |
| `vedit studio` | Serves Studio on 127.0.0.1 only. It holds the workspace lease while it runs. |

`vedit agent` turns the goal into a brief with a deterministic parser. Story
planning with understanding of the footage is Muse Spark's job: use the MCP
tools or Studio for it.
