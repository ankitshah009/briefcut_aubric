---
name: briefcut
description: Edit real footage with Briefcut. It imports clips, plans a story with Meta's Muse Spark, renders on this machine with FFmpeg, and proves the delivered file with receipts. It needs a Meta API key. Use it to cut a talk or a set of clips into a short, add captions, music or a logo, make several shorts from one long clip, find a moment, or turn a script into a narrated video. Do not use it to write FFmpeg by hand, for image-only design work, or to publish to social platforms, which Briefcut does not do.
metadata:
  version: "1"
---

# Briefcut

Briefcut is a video editor that runs on Meta models. Muse Spark plans and
reviews the story, SAM 3.1 tracks and cuts out the subject, Meta Voice
Transcribe writes the captions, and Muse Image draws covers and scene pictures.
FFmpeg renders on this machine. There is no local mode and no local model: a
Meta API key is required. Its MCP server (`briefcut`) runs the same project
manager as Studio and the Mac app. Every finished render carries receipts:
named checks plus an ffprobe of the delivered file. The receipts are the
proof, not the job status.

## Connect only when needed

The Claude Code plugin registers the `briefcut` server and this skill. A
manual install registers the server with `claude mcp add` or the repository's
`.mcp.json`. Installing the skill alone adds no tools.

- If `briefcut_*` tools are listed, go straight to the task. Do not run
  `vedit doctor` or other setup before every job.
- Use the tool list and input schemas the server actually advertises. Tools
  marked "when listed" below exist in the current server but not in older ones.
- `briefcut_status` (when listed) reports `ready` and, when something is
  missing, `missing` in plain words. Both requirements must hold: `meta.available`
  (a Meta API key is set and Meta calls are allowed) and `render.ready`
  (FFmpeg). It also lists the workspace and the ids `briefcut_edit` accepts.
  It makes no network call. Call it once at the start, not before every step.
- If Meta is not available, stop and tell the user: create a key at
  https://dev.meta.ai, then paste it in Briefcut Settings (or set
  `MODEL_API_KEY` with `VEDIT_META_ENABLED=1`). Do not write keys or change
  settings yourself.
- Projects live in `<workspace>/vedit-sessions/`. The workspace is
  `VEDIT_WORKSPACE` when set, otherwise the folder the server started in.
- One process at a time holds a workspace. The error "Another Studio is
  already processing this workspace" means Studio, the Mac app, or another
  agent session has it. Tell the user. They can close that one, or restart
  this server with a different `VEDIT_WORKSPACE`. Do not retry in a loop.
- If the tools are missing or a call fails before it reaches Briefcut, read
  [connection and recovery](references/connection-and-recovery.md).

## Route the input first

Pick the first tool from what the user gave you. Paths must be absolute.

| The user has | Do this |
| --- | --- |
| One clip | `briefcut_import_clip(path)`, then `briefcut_create_story(paths, goal, ...)` |
| Several clips or photos | Import each file (one call per file), then one `briefcut_create_story` with every returned path (1 to 30 files, 4 GB each) |
| A long clip and a request for several shorts | Make the story first and wait for `planned`, then `briefcut_make_shorts(job_id, count)` with a count of 2 to 5 |
| "Find the part where ..." | `briefcut_find_moments(job_id, query)` on a project that already has a plan |
| A written script | `briefcut_script_to_video(script, images)`; with no images, Muse Image draws each scene |
| Music, a logo, or a picture to add | `briefcut_add_asset(job_id, path)` (when listed), then use its `id` in `briefcut_edit` |
| A planned cut to change | Read it with `briefcut_timeline`, change it with `briefcut_edit` (both when listed), or render an edited copy of `job.plan` with `briefcut_render(job_id, plan)` |
| A rendered cut to improve | `briefcut_revise(job_id, mode="auto")` applies the automatic fixes from the receipts |
| A brief-level edit (silence cut, music duck, export presets, podcast audio) | Use the CLI; see [CLI and briefs](references/cli-and-briefs.md) |

The story flow is the same every time:

1. `briefcut_create_story` queues planning and returns a job.
2. `briefcut_wait(job_id)` until the status is `planned`.
3. Read the cut with `briefcut_timeline(job_id)` when listed, or from
   `job.plan`. Show it to the user. Render when they approve, or at once if
   they asked for a finished video.
4. `briefcut_render(job_id)`, or `briefcut_edit(..., render=true)` for a
   changed cut.
5. `briefcut_wait(job_id)` until the status is `complete`.
6. `briefcut_receipts(job_id)`, then `briefcut_artifact_path(job_id, "video")`.

Useful `briefcut_create_story` fields: `goal` (what the video should do),
`target_duration` (0.5 to 180 seconds, default 30), `aspect` (`9:16`, `16:9`,
or `1:1`, default `9:16`), `captions` (default on), and `subject`.

A plan has `segments` (source `start` and `end` in seconds, with `label` and
`reason`), `title`, `caption`, `hashtags`, and an `edit` block. Briefcut
validates every changed plan or edit before it renders and returns a tool
error that names the bad field.

## Work summary-first

- `briefcut_timeline` starts with `detail="summary"`. Ask for `segments`,
  `spans`, `captions`, or the full `edit` only when the summary leaves a
  question open, and page long lists with `offset` and `limit`.
- `briefcut_edit` is a dry run unless `render=true`. Show the user its
  `changes`, the new length, and any `warnings` before you render.
- Summarize jobs for the user: status, planned length, segment times with
  labels, and title. Do not paste whole job objects.
- `briefcut_wait` times out after `timeout_seconds` (default 600). The job
  keeps running. Call `briefcut_wait` again. Do not create a duplicate job.
- Use `briefcut_list` to find earlier projects before starting a new one.
- Read files from disk with the path `briefcut_artifact_path` returns
  (`source`, `video`, `cover`, `bundle`, `brief`, `report`). Do not ask the
  server to inline them.
- Pass warnings on to the user.
- A `failed` job carries an `error`. Fix the cause, then `briefcut_retry`.
  `briefcut_cancel` stops a running job and keeps the saved plan.

## The proof rule

A render is proven only by its receipts and the file itself.

- Over MCP: `briefcut_receipts` must show `passed == total` and
  `proof.has_video` true. Check that `proof.duration`, `proof.width`, and
  `proof.height` match what the user asked for, and `proof.has_audio` when
  the video should have sound.
- On the CLI: read `report.json` and confirm duration, width and height,
  `has_audio`, `has_video`, and color.
- To probe a file yourself, run `vedit probe /absolute/file.mp4` (from a
  checkout, `uv run vedit probe ...`), or `ffprobe`.
- A `complete` status, an exit code of 0, or a progress bar is not proof.
- Stream checks prove a playable file, not a good edit. Say so. Look at the
  cover or frames when the user cares how it looks.
- Exporting a package is not publishing. Briefcut never posts anywhere.

## Meta, consent, and spend

Every project runs on Meta's Model API with the user's key. `provider` and
`caption_provider` are always `meta`; the create tools refuse `"local"`.
Creating a story sends a 960x540 copy of the whole project and the brief to
Muse Spark, and, with `captions` on (the default), the audio to Meta Voice
Transcribe. The full list is in the repository's `docs/PRIVACY.md`.

Later sends, all on the same project: `review=true`, `generate_cover=true`,
`briefcut_revise` with `mode="note"`, footage search and shorts, a script
with no images, and, when listed, `briefcut_edit` with a new caption language
(caption text) or a cutout (a copy of the footage). A dry run's `meta` field
names each one; tell the user before you render it.
`briefcut_generate_background` needs `consent=true` (the prompt words, about
$0.01). `mode="director"` applies patches a Meta review already returned.
Each project has a spending cap, `budget_usd` (default 0.25, at most 5).

The consent rule: footage goes to Meta only for a project the user asked you
to make.

- Before the first story, say that the footage goes to Meta on the project's
  budget.
- Set `consent` to true only after the user agreed to that specific send.
- If a tool says Meta is not enabled, tell the user how to add the key. Do not
  set `VEDIT_META_ENABLED`, write keys, or change Briefcut settings yourself.
- Leave contributor mode as the user set it. In that mode Meta may train on
  what is sent.

## Never

- Hand-edit `timeline.otio.json`. It is interchange, not the program, and
  editing it renders nothing. Change the plan or edit (MCP) or the brief (CLI).
- Write FFmpeg commands by hand or invent flags. A missing operation belongs
  in `src/vedit/compile.py` `OPS` with a test.
- Call paid caption, enhance, or "AI editor" APIs, or any model that is not
  Meta's. Meta is the only model provider, and only with consent.
- Delete files under `vedit-sessions/` while a job is active.
- Claim a render worked without its receipts or a probe of the file.

## Finish the task

Give the user the absolute path of the video, its proven duration and frame
size, whether it has audio, and the checks passed (`passed` of `total`).
Name the revision when there is more than one. Mention any warnings and, for a
Meta project, the spend from the budget ledger. If the receipts list patches,
offer `briefcut_revise(job_id, mode="auto")` for the automatic fixes. Version
N stays available after a revision.
