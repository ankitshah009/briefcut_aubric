# Briefcut campaign workflow

**Briefcut by Aubric.ai** turns real footage into an editable story, a cover, post copy, and a downloadable delivery package. Rendering runs on the local machine. Optional Meta integration adds footage understanding and subject tracking; the `vedit` compiler still owns the actual edit. Curated visual effects are available inside the same workflow. See [Brand](BRAND.md) and [Spatial effects](SPATIAL_EFFECTS.md).

## Run locally

Use [uv](https://docs.astral.sh/uv/) (it provisions CPython 3.13) and an FFmpeg build with `drawtext` and `subtitles`. From the repository root:

```sh
./scripts/studio.sh --port 8765 --open
```

The launcher creates `.venv` with uv when it is missing (`uv sync`), checks dependencies, runs from the checkout, defaults `VEDIT_FFMPEG` to `ffmpeg-full` when installed, and uses the first `ggml-*.bin` it finds in the shared `~/.vedit/models/` (override with `VEDIT_MODELS_DIR`) or, failing that, the checkout's `.vedit-models/` (`ggml-base.en.bin` preferred). `scripts/demo_run.py`, `vedit doctor`, and the captions module use the same order. The model is not included in the repository or app bundle; one download under `~/.vedit/models/` serves every checkout.

Stop if a required doctor check fails. A missing Whisper model disables captions but does not prevent a video render. To launch Python directly with a local model:

```sh
export VEDIT_WHISPER_MODEL="$HOME/.vedit/models/ggml-base.en.bin"
uv run vedit doctor
uv run vedit studio --port 8765 --open
```

The multilingual base model and a requested language are separate choices. Selecting a language does not translate speech or dub the video.

Studio binds to `127.0.0.1` or `localhost`. It is a local application, not a public, authenticated hosting service. The existing CLI, Python API, and legacy session endpoints remain available.

**Briefcut.app** keeps `~/Library/Application Support/Vedit` as its workspace so existing projects remain available; set an absolute `VEDIT_WORKSPACE` to override it. Downloads open a native Save dialog and reveal the completed file in Finder. The app writes to a temporary sibling file and replaces the destination only after the download completes. Python 3.13+ and FFmpeg remain external dependencies. `VEDIT_PYTHON` can select a supported installed interpreter; otherwise the app discovers an installed stable interpreter. Caption-model and Meta configuration must be present in the app's environment; the desktop app does not inherit the checkout launcher's model discovery.

## Use the workspace

1. Choose one video under 200 MB and no longer than 20 minutes. Studio uploads it to the local server once and reuses the stored source.
2. Describe the outcome, then choose a duration, frame shape, and provider. Editing phrases in the brief are honoured at render time (see "Brief-driven edits" below); the "Product story, edited" chip is an example brief that asks for pause trimming, a title card, and a fade out.
3. Create the story plan. Review the selected source ranges and edit their times, labels, title, and post copy. Every proposed cut is snapped to a transcript sentence boundary when a transcript exists; the reason text says when an edge was moved.
4. Optionally choose a visual effect and its interval in the assembled story. Approve the plan by starting the render. Progress belongs to the job, so a page refresh does not start a second encode.
5. Play the result and read "The receipts": the probe of the file and the list of checks with jump-to-time. Download the video or the complete package.
6. Revise without losing the verified version: "Apply director's fixes" (Meta review patches), "Apply automatic fixes" (deterministic lint patches), or a typed director's note (Meta mode). Each renders the next version; version tabs switch the player, receipts, and downloads between versions.

The plan is editorial input. A successful render verifies the technical file; it does not prove that the story is compelling, captions are accurate, or a campaign will drive sales. The automatic checks are measurements against the stored transcript and target; the director's checks are a model's opinion and are labeled as such.

### Brief-driven edits

`src/vedit/brief_ops.py` reads the goal with the deterministic intent parser and inserts ops after the story assembly and before captions. Recognised phrases: "trim/cut/remove the pauses" or "cut silence" (`cut_silence`, pauses over 0.6 s inside the selected moments; captions are transcribed from the trimmed clip); "title card" or `title "…"` (a 2.5 s centered title, the quoted text or the plan's title); "fade in", "fade out", "fade to black" (0.5 s); "denoise"; "no loudnorm" or "keep the original loudness"; `hook: …` (3 s upper-third text) and `cta: …` (3 s lower-third text over the last seconds). Requests that cannot be honoured, such as a watermark, a music bed, or pause trimming on silent footage, are listed under `ignored_edits`. The plan carries `decisions` (one sentence per applied edit) and `ignored_edits`; Studio shows both under the plan, and the receipts check "auto: requested edits applied" fails when anything was ignored. Unknown words are ignored silently.

### Receipts, versions, and revisions

After export verification, `CampaignManager._grade` runs `vedit.lint.lint_plan` on the approved plan: cuts on sentence boundaries (within 0.15 s), duration within 50%–115% (+0.5 s) of the target, moments inside the source and non-overlapping, caption coverage of at least 80% of the selected speech (when captions were burned), plus the requested-edits check. In Meta mode with `review: true`, Muse Spark's review adds `director:` checks (one per requirement in the goal, then hook, mid-sentence cuts, pacing, legibility) and structured patches. Failing automatic checks propose `source: "auto"` patches; the director's are `source: "director"`.

The completed job's `result` carries `checks` (`name`, `passed`, `at` in finished-video seconds or null, `note`), `passed`, `total`, and `patches` (`op` in `trim_start` / `trim_end` / `remove` / `move_before`, 1-based `segment`, `seconds`, `reason`, `source`). `job.versions` lists one entry per completed render, oldest first: `revision`, `status` (`verified` or `failed`), `source` (`approved`, `director`, `auto`, or `note`), `proof`, `checks`, `passed`, `total`, `hook_score`, `verdict`, `applied_patches`, `note`, `timings.render_seconds`, and `video_url` / `report_url` / `cover_url` pinned with `?v=N`. `scripts/demo_run.py` writes the receipts and the version table into `docs/evidence/`.

Floating frame, Perspective sway, and Push-in reveal use local perspective and framing effects. They run after reframing and before captions. Their timing refers to the assembled edit, and the finished render shows the effect. They do not require Meta, infer scene depth, or create a 3D model. See [Spatial effects](SPATIAL_EFFECTS.md) for controls, supported values, and examples.

## Local and Meta modes

| Mode | What it does | What it does not establish |
| --- | --- | --- |
| Local | Measures scene cuts, speech ranges, and, when a local Whisper model is installed, a transcript; then selects non-overlapping moments that fit the target length, each with the measurement that chose it. Renders your edited ranges with local tools. | Signal analysis is not understanding. It cannot judge whether a moment is interesting, true, or on-message; it only knows where picture and speech change and which brief words were spoken. |
| Meta | Requests a structured story plan from footage and can request subject geometry for reframing. | A model's explanation is not independent evidence. Review the ranges against the source. |

The local planner (`src/vedit/story.py`, also `uv run vedit story CLIP --goal "..." --target 20`) is deterministic and offline. It detects scene cuts on a 320-pixel downscale (strong changes first; when there are none, sparse weaker hard cuts at least 2 s apart), inverts `silencedetect` into speech ranges, and runs the local transcriber only when one is installed and the clip is short enough to keep planning fast. Transcript cues become sentences whose edges are clamped to the speech actually measured inside them. Candidates are speech ranges split at scene cuts, merged into 1.5–12 second pieces, and snapped to those sentences; each is scored on speech coverage, overlap between transcript words and your brief, a scene-change bonus, and a bonus for the first and last spoken moments. For a brief that asks how something is made, selection spreads moments across scenes (or equal time spans when no scene cuts were found) instead of spending the budget on consecutive sentences. Titles, labels, hashtags, and post copy come from the brief's subject and the spoken sentences. The plan carries an `analysis` object (scene times, speech ranges, transcript cues, sentences, notes) for review, and every segment states its `kind` and `score`. When the source already fits the target, the plan is the whole clip; with no speech or scenes, it says so in a warning and falls back to the opening. None of this is a model call, and the summary says so.

In Meta mode the same local measurement runs first and the transcript, with its sentence boundaries listed as preferred cut points, is attached to the Muse Spark prompt as source-clock data. Spark's ranges are then snapped to those sentences locally (`snap_to_sentences`), so the reason text on a Meta plan can say "end moved back to sentence boundary at 4.6 s".

There are no paid model calls in the default local workflow. Keep `VEDIT_META_ENABLED` unset while developing without an approved cloud budget. Configure a server-side `MODEL_API_KEY` and explicitly enable Meta only after confirming the account's credits or billing arrangement. Never put a key into the browser, a brief, or a committed file.

Cloud inference sends an analysis copy and instructions to the configured Meta service. Local rendering does not make those model requests private or offline. A local cover is a frame from the video; generated imagery is a separate operation and must be identified as such.

The adapter uses Standard mode with `muse-spark-1.3`, `sam-3.1`, `muse-image-1.0`, and `muse-voice-transcribe-1.0` by default. It never switches modes silently: set `VEDIT_META_MODE=contributor` to plan stories with `muse-spark-1.3-contributor` ($0.10 / $0.20 per million tokens instead of $1.25 / $4.25). Contributor mode permits Meta to use the submitted content to train future models, so choose it deliberately. SAM, Muse Image, and Voice Transcribe have no contributor variant and keep their standard prices. For authorized use, set these in your own terminal before launching Studio:

```sh
# Set MODEL_API_KEY through your local secret-management workflow first.
export VEDIT_META_ENABLED=1
./scripts/studio.sh --port 8765 --open
```

Captions can also come from Meta. With `captions: true` and `caption_provider: "meta"`, the render extracts only the approved speech as a mono 16-bit, 16 kHz WAV in playback order and sends it to `muse-voice-transcribe-1.0` (`POST /v1/asr/transcribe`, $0.18 per hour of audio, billed in whole seconds). The returned turn-level timestamps already sit on the finished edit's clock, so no remapping is needed; the cues are written to `captions.srt` and `captions.ass` and burned locally. Both providers pass through `polish_cues` (`src/vedit/captions.py`): doubled words collapse, fragments merge into sentence-level cues when the gap is under 0.35 s and the text fits 84 characters, each cue starts with a capital, and lines wrap into at most two balanced lines of 42 characters.

Director's notes (`review: true`, Meta mode) send a 540p copy of the finished cut to Muse Spark after verification and return a verdict, hook score, strengths, timed fixes, a caption suggestion, structured `checks`, and structured `patches` (ledger operation `review`). A failed review is a warning; the verified file is unchanged. A typed note goes through `meta.patch_plan` as text only (ledger operation `patch`). The request is refused before upload when the selection exceeds 10 minutes or 32 MB, its cost is reserved against the same campaign ledger under the `transcribe` operation, and a failure stops the render with an actionable error rather than silently switching to local Whisper. Turns are sentence-level, not word-level, and selecting a language only biases recognition; it does not translate. Local Whisper remains the default and needs no credentials. Every stored upload also carries a one-hour server-side expiry as a safety net behind the adapter's own deletion.

`budget_usd` is a **total campaign ceiling**. The campaign HTTP API accepts $0.01–$5. All operations and revisions share the same project ledger. Before an inference request, the adapter adds its worst-case cost to prior reservations and refuses a request that would exceed the cap. Failed or uncertain calls keep their reservations; successful calls do not automatically refund unused reserves. An identical cached result adds no inference charge. Separate campaigns have separate caps, so this is not an account-wide spending limit.

Story planning counts input tokens before inference and reserves against its maximum output. Tracking estimates the cost from frames before uploading. A successful result is cached inside its project using the source hash and operation settings.

`meta-usage.json` records reservations and usage estimates. An ambiguous inference timeout is not retried automatically because the first request may already have incurred a charge. Compare the local ledger with provider billing; it is not a billing statement. Prices are encoded as of October 29, 2026 and must be reviewed when the provider changes them.

The adapter attempts to delete its remote upload after each operation. If deletion fails, `meta-cleanup.json` retains the remote file identifier and an actionable warning. Complete that cleanup rather than assuming every failed operation removed its media.

Studio accepts locally decodable video containers within its source limits, including MOV and WebM. In Meta mode it uses vedit to create a compatible 960×540, 30 fps MP4 analysis copy. The direct story adapter accepts MP4, and the direct tracking adapter accepts supported MP4/MOV footage with at most 15,000 frames; Studio's proxy handles that format boundary. Generated covers take a PNG, JPEG, or WebP still extracted from the source. Tracking is refused when the chosen subject's coverage is too incomplete to justify following it. These adapters do not provide generative video or dubbing.

## Architecture

```text
Local upload
    → durable campaign job
    → local planner or Meta adapter
    → validated, editable story plan
    → approved YAML brief
    → vedit compiler → project.json + render.sh
    → local render → probe → report.json → receipts (lint + director's checks)
    → video + cover + post copy + delivery archive
    → revise (director | auto | note) → patches → relint → version N+1
```

The model returns data, never shell commands. Source ranges must be finite and inside the clip. Story analysis uses a full-source proxy whose timestamps match the original. Subject tracking uses a separate proxy containing only the approved segments in their chosen playback order, so its frame cost follows the selected duration rather than the full source.

Tracking boxes are mapped out of the analysis copy's letterbox padding and back into the original displayed frame, including rotation metadata. For the final render, vedit selects the approved ranges from the original source, then applies the track on that assembled timeline. It does not export the small analysis copy. Delivery still involves encoding; this is not a lossless pipeline. Changing segment ranges invalidates the corresponding tracking cache. Local mode creates no analysis proxies and makes no model requests.

The durable job record separates planning from rendering. An approved plan is the render input; changing text in the browser cannot silently change an already running render. Download handlers expose named artifacts rather than arbitrary paths.

The database is `vedit-sessions/campaigns/campaigns.sqlite3`, relative to the workspace; each job has its own folder below `vedit-sessions/campaigns/`. Analysis copies live under that job's `analysis/` folder. The checkout launcher stores these in the checkout, while the macOS app uses its Application Support workspace. One worker processes heavy operations serially. A browser refresh reconnects to the saved job. A server restart marks unfinished jobs as interrupted, rather than claiming they continued. Explicit retry preserves the approved plan but may repeat encoding.

Starting a new render keeps the last verified version available until a replacement passes verification. The job records which revision produced that result. A failed or cancelled revision must not be mistaken for a new successful export. Before large processing stages, the app estimates required disk space and refuses to start when the workspace drive lacks room.

Cancellation signals the active local process group, including nested caption and encoding processes. A Meta request already in flight may finish or time out before cancellation is observed; the worker starts no subsequent step. The job's `verified_revision`, `result.revision`, and `result.stale` distinguish the available verified export from a newer attempt. `VEDIT_MIN_FREE_DISK_MB` sets the extra disk-space floor, defaulting to 64 MiB, in addition to the estimated work files and archive.

## Local HTTP API

Clients use the same local origin as Studio. Send JSON mutations with `Content-Type: application/json`; do not expose this server through a public reverse proxy. Source uploads use `POST /api/upload?name=clip.mp4` with an `application/octet-stream` body and `Content-Length`. Reuse the returned `paths` when creating a campaign.

| Method and route | Result |
| --- | --- |
| `GET /api/capabilities` | Local and Meta readiness, caption availability, and the supported effects catalogue. |
| `POST /api/campaigns` | Queue planning; returns HTTP 202 with `{ "ok": true, "job": ... }`. |
| `GET /api/campaigns` | Saved jobs in `{ "ok": true, "jobs": [...] }`. |
| `GET /api/campaigns/{id}` | Current job, its plan, progress, result, and error. |
| `POST /api/campaigns/{id}/render` | Approve a stored or edited plan and queue rendering. |
| `POST /api/campaigns/{id}/revise` | Apply structured patches to the verified plan and queue version N+1. Body `{ "mode": "director" }`, `{ "mode": "auto" }`, or `{ "mode": "note", "note": "..." }`. |
| `POST /api/campaigns/{id}/retry` | Retry an eligible failed or interrupted operation. |
| `POST /api/campaigns/{id}/cancel` | Request cancellation. |
| `GET /api/campaigns/{id}/artifact?name=video&v=N` | Read an allowlisted artifact. Names: `source`, `video`, `cover`, `bundle`, `brief`, `report`. `v` selects a verified version; omit it for the current one. |

A local planning request looks like this. Use the actual path returned by upload:

```json
{
  "paths": ["<uploaded source path>"],
  "goal": "Make a short product demonstration",
  "provider": "local",
  "target_duration": 20,
  "aspect": "9:16",
  "captions": false,
  "language": "en",
  "subject": "",
  "budget_usd": 0.25,
  "generate_cover": false,
  "effect": null
}
```

Poll the returned job ID until its `status` is `planned`. Other statuses are `queued`, `analyzing`, `rendering`, `complete`, `failed`, and `cancelled`. The editable plan contains `title`, `summary`, `segments`, `caption`, `hashtags`, `cover_prompt`, `provider`, `usage`, `analysis` (scenes, speech, transcript, sentences, notes), `decisions`, and `ignored_edits`. Each segment has source-clock `start` and `end` seconds, a `label`, a `reason`, a `kind`, and a `score`. Submit the edited object as `{ "plan": ... }` to the render route, or send `{}` to use the stored plan. Use URLs from the completed job's `result` or `versions` for downloads.

Frame shapes are `9:16`, `16:9`, and `1:1`. Target duration is 0.5–180 seconds. An edited plan allows one to eight segments, at least 0.1 seconds each, and no more than 180 seconds in total. A generated cover requires Meta mode and explicit `generate_cover: true`; the default package always includes a local cover frame. A nonempty `subject` enables tracking in Meta mode. Local mode uses a center crop and displays a warning if a subject was requested.

An optional `effect` object selects a curated treatment, for example `{ "preset": "float", "intensity": 0.55, "start": 0, "duration": 3 }`. It can be supplied when creating the campaign or approving the render. Its interval must fit the assembled story; `null` disables it. The approved effect is included in the render record.

HTTP 202 means the operation was accepted; it does not mean the video is complete. Errors use `{ "ok": false, "error": "..." }`. Retry only after reading the error, especially when a cloud request may have incurred a charge.

### Revise

`POST /api/campaigns/{id}/revise` renders the next version from structured patches and keeps the previous verified version. The job is first re-checked, then the pure patcher (`vedit.lint.apply_patches`) applies the patches without ever raising: a patch that would leave a moment shorter than 0.5 s, overlapping another, outside the source, or pointing at a removed moment is recorded with `skipped` set and otherwise ignored. For `director` and `note`, the deterministic lint runs again on the patched plan and its own patches are applied last, so a model patch that lands inside a sentence is snapped back to a boundary before anything renders. The result is validated like any approved plan, then queued with the campaign's effect and tracking settings.

| Mode | Source of patches | Needs |
| --- | --- | --- |
| `director` | `result.patches` entries with `source: "director"` from the last Meta review. | A verified version with director's patches. |
| `auto` | `result.patches` entries with `source: "auto"` from the lint. | A verified version with failing automatic checks that have patches. |
| `note` | `meta.patch_plan` turns `note` (≤ 500 characters) into patches with a text-only Muse Spark call; nothing is uploaded. Ledger operation `patch`. | A campaign that used Meta (director, Meta captions, or review) and an enabled server. |

Response: HTTP 202 with the queued job; `revision` is N+1, `revision_source` is the mode, `applied_patches` lists every patch with `before`, `after`, `reason`, `source`, and `skipped`, and `note` holds the director's note. When the render verifies, `versions` gains the new entry and `result` describes it; on failure the new entry has `status: "failed"` and the previous verified version remains the download.

HTTP 409 is returned, with the reason in `error`, when: a render is still active; the job has no plan or source; no version has been verified yet; the requested mode has no patches to apply; `note` is used on a Local campaign or when Meta is not configured and enabled; Meta cannot turn the note into any patch, or the call fails (the message is passed through; the verified version is unchanged); or every patch was skipped (`No fixes could be applied safely (...)`). An unknown mode or a missing note is HTTP 400. `GET` on the route is 405.

`scripts/demo_run.py --revise director|auto|note [--note "..."]` runs one revision after the first verified render and records `revise` (mode, note, applied patches, revision, checks, proof, review) and `timings_seconds.revise` in the evidence file.

## Verify a delivery

Open the exported `report.json` and confirm duration, dimensions, video and audio streams, and color metadata. Listen to the output and inspect the beginning, cut boundaries, and end. For a downloaded video, run:

```sh
uv run vedit probe /path/to/downloaded-video.mp4
```

Check the archive itself: `video.mp4` should play, the cover should open, and `post.txt` should match the approved text. The package also includes `story.json`, `brief.yaml`, and `report.json`. Keep the generated `project.json` and `render.sh` from the job's render folder when reproducing an edit. `timeline.otio.json` is interchange data; editing it does not change the executable program.

## Failures and limits

| Symptom | Action |
| --- | --- |
| Captions unavailable | Install a real local Whisper model, set `VEDIT_WHISPER_MODEL`, and restart Studio. |
| Meta unavailable | Check server-side credentials and the explicit enable switch. Continue with Local if cloud access is not authorized. |
| Model request exceeds budget | Review the campaign's retained reservations and reduce input or optional work. A new campaign has a separate cap; creating one does not erase previous charges. |
| No tracked subject | Use a more specific subject or remove tracking and review the framing manually. Do not treat a center crop as successful tracking. |
| Interrupted job | Review the retained job and its error before retrying. Do not infer completion from a progress bar. |
| Render failed | Read the job error and generated program; correct the source or plan, then retry. |
| Insufficient disk space | Free space on the workspace drive, then retry. The previous verified version remains available. |
| Video looks wrong despite passing verification | Revise the plan. Stream checks establish playable media, not editorial quality. |

Campaign export does not publish to Instagram, Facebook, WhatsApp, or Threads. Platform login, approval, publication, and engagement attribution are separate product work. Exporting a package is never reported as publishing it.

Sources and session artifacts stay on disk until removed. Keep customer media out of source control, and do not delete active job files.

## MCP for coding agents

`uv run vedit mcp` serves the same campaign manager as Studio over the Model Context Protocol on stdio, so Claude Code, Cursor, or any MCP client can import a clip, plan, render, and read the receipts without the HTTP server. It needs the `mcp` package (`uv sync` installs it; pip users take `vedit[mcp]`). The workspace is `VEDIT_WORKSPACE` when set, otherwise the current directory, and one process at a time holds a workspace's lease: point the agent at a different `VEDIT_WORKSPACE` than a running Studio, or stop Studio first.

Register it once per project:

```sh
claude mcp add briefcut -e VEDIT_WORKSPACE=/path/to/workspace -e VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg -- uv run --directory /path/to/briefcut vedit mcp
```

or in a client's JSON config:

```json
{
  "mcpServers": {
    "briefcut": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/briefcut", "vedit", "mcp"],
      "env": {"VEDIT_WORKSPACE": "/path/to/workspace", "VEDIT_FFMPEG": "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"}
    }
  }
}
```

| Tool | Does |
| --- | --- |
| `briefcut_import_clip(path)` | Copies a local video (≤ 200 MB, must probe as video) into a fresh `vedit-sessions/<id>/inbox/` folder; returns `paths`, `bytes`, duration, and frame size. |
| `briefcut_create_story(paths, goal, provider="local", target_duration=30, aspect="9:16", captions=true, caption_provider="local", language="en", subject="", budget_usd=0.25, generate_cover=false, review=false, effect=null)` | Queues a plan; same fields as `POST /api/campaigns`. Returns the job. |
| `briefcut_wait(job_id, timeout_seconds=600)` | Polls every 0.5 s until `planned`, `complete`, `failed`, or `cancelled`; a timeout is a tool error and the job keeps running. |
| `briefcut_get(job_id)`, `briefcut_list()` | The public job, or `{"jobs": [...]}` newest first. |
| `briefcut_render(job_id, plan=null)` | Renders the saved plan or an edited copy of `job.plan`. |
| `briefcut_revise(job_id, mode, note=null)` | Version N+1 from `auto`, `director`, or `note` patches (see Revise above). |
| `briefcut_receipts(job_id)` | `revision`, `passed`/`total`, named `checks`, ffprobe `proof`, `patches`, `review`, `versions`. |
| `briefcut_artifact_path(job_id, name, revision=null)` | Absolute path on disk for `source`, `video`, `cover`, `bundle`, `brief`, or `report`, so the agent reads the file itself. |
| `briefcut_cancel(job_id)`, `briefcut_retry(job_id)` | Stop a running job; re-queue a failed or cancelled one. |

Every `CampaignError` comes back as a tool error with the same message the HTTP API would return; unexpected failures are reported without their traceback. A render is verified only when `briefcut_receipts` shows `passed == total` and `proof.has_video`; a `complete` status alone is not proof.
