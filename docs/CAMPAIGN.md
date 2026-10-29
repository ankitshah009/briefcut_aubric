# Briefcut campaign workflow

**Briefcut by Aubric.ai** turns real footage into an editable story, a cover, post copy, and a downloadable delivery package. Rendering runs on the local machine. Optional Meta integration adds footage understanding and subject tracking; the `vedit` compiler still owns the actual edit. Curated visual effects are available inside the same workflow. See [Brand](BRAND.md) and [Spatial effects](SPATIAL_EFFECTS.md).

## Run locally

Use Python 3.13 or newer and an FFmpeg build with `drawtext` and `subtitles`. From the repository root, with its virtual environment installed:

```sh
./scripts/studio.sh --port 8765 --open
```

The launcher checks dependencies, runs from the checkout, and uses `.vedit-models/ggml-base.bin` automatically when present. This workspace has a roughly 141 MiB multilingual base model installed locally; the model is not included in the repository or app bundle. A fresh checkout still needs its own model for captions.

Stop if a required doctor check fails. A missing Whisper model disables captions but does not prevent a video render. To launch Python directly with a local model:

```sh
export VEDIT_WHISPER_MODEL="$PWD/.vedit-models/ggml-base.bin"
.venv/bin/python -m vedit doctor
.venv/bin/python -m vedit studio --port 8765 --open
```

The multilingual base model and a requested language are separate choices. Selecting a language does not translate speech or dub the video.

Studio binds to `127.0.0.1` or `localhost`. It is a local application, not a public, authenticated hosting service. The existing CLI, Python API, and legacy session endpoints remain available.

**Briefcut.app** keeps `~/Library/Application Support/Vedit` as its workspace so existing projects remain available; set an absolute `VEDIT_WORKSPACE` to override it. Downloads open a native Save dialog and reveal the completed file in Finder. The app writes to a temporary sibling file and replaces the destination only after the download completes. Python 3.13+ and FFmpeg remain external dependencies. `VEDIT_PYTHON` can select a supported installed interpreter; otherwise the app discovers an installed stable interpreter. Caption-model and Meta configuration must be present in the app's environment; the desktop app does not inherit the checkout launcher's model discovery.

## Use the workspace

1. Choose one video under 200 MB and no longer than 20 minutes. Studio uploads it to the local server once and reuses the stored source.
2. Describe the outcome, then choose a duration, frame shape, and provider.
3. Create the story plan. Review the selected source ranges and edit their times, labels, title, and post copy.
4. Optionally choose a visual effect and its interval in the assembled story. Approve the plan by starting the render. Progress belongs to the job, so a page refresh does not start a second encode.
5. Play the result and inspect its verification details. Download the video or the complete package.

The plan is editorial input. A successful render verifies the technical file; it does not prove that the story is compelling, captions are accurate, or a campaign will drive sales.

Floating frame, Perspective sway, and Push-in reveal use local perspective and framing effects. They run after reframing and before captions. Their timing refers to the assembled edit, and the finished render shows the effect. They do not require Meta, infer scene depth, or create a 3D model. See [Spatial effects](SPATIAL_EFFECTS.md) for controls, supported values, and examples.

## Local and Meta modes

| Mode | What it does | What it does not establish |
| --- | --- | --- |
| Local | Starts with the requested number of seconds from the beginning of the source, then renders your edited ranges with local tools. | It does not understand scenes or identify the strongest story moment. |
| Meta | Requests a structured story plan from footage and can request subject geometry for reframing. | A model's explanation is not independent evidence. Review the ranges against the source. |

There are no paid model calls in the default local workflow. Keep `VEDIT_META_ENABLED` unset while developing without an approved cloud budget. Configure a server-side `MODEL_API_KEY` and explicitly enable Meta only after confirming the account's credits or billing arrangement. Never put a key into the browser, a brief, or a committed file.

Cloud inference sends an analysis copy and instructions to the configured Meta service. Local rendering does not make those model requests private or offline. A local cover is a frame from the video; generated imagery is a separate operation and must be identified as such.

The adapter uses Standard mode with `muse-spark-1.3`, `sam-3.1`, and `muse-image-1.0`. It does not silently switch to a cheaper data-contribution mode. For authorized use, set these in your own terminal before launching Studio:

```sh
# Set MODEL_API_KEY through your local secret-management workflow first.
export VEDIT_META_ENABLED=1
./scripts/studio.sh --port 8765 --open
```

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
    → local render → probe → report.json
    → video + cover + post copy + delivery archive
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
| `POST /api/campaigns/{id}/retry` | Retry an eligible failed or interrupted operation. |
| `POST /api/campaigns/{id}/cancel` | Request cancellation. |
| `GET /api/campaigns/{id}/artifact?name=video` | Read an allowlisted artifact. Names: `source`, `video`, `cover`, `bundle`, `brief`, `report`. |

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

Poll the returned job ID until its `status` is `planned`. Other statuses are `queued`, `analyzing`, `rendering`, `complete`, `failed`, and `cancelled`. The editable plan contains `title`, `summary`, `segments`, `caption`, `hashtags`, `cover_prompt`, `provider`, and `usage`. Each segment has source-clock `start` and `end` seconds, a `label`, and a `reason`. Submit the edited object as `{ "plan": ... }` to the render route, or send `{}` to use the stored plan. Use URLs from the completed job's `result` for downloads.

Frame shapes are `9:16`, `16:9`, and `1:1`. Target duration is 0.5–180 seconds. An edited plan allows one to eight segments, at least 0.1 seconds each, and no more than 180 seconds in total. A generated cover requires Meta mode and explicit `generate_cover: true`; the default package always includes a local cover frame. A nonempty `subject` enables tracking in Meta mode. Local mode uses a center crop and displays a warning if a subject was requested.

An optional `effect` object selects a curated treatment, for example `{ "preset": "float", "intensity": 0.55, "start": 0, "duration": 3 }`. It can be supplied when creating the campaign or approving the render. Its interval must fit the assembled story; `null` disables it. The approved effect is included in the render record.

HTTP 202 means the operation was accepted; it does not mean the video is complete. Errors use `{ "ok": false, "error": "..." }`. Retry only after reading the error, especially when a cloud request may have incurred a charge.

## Verify a delivery

Open the exported `report.json` and confirm duration, dimensions, video and audio streams, and color metadata. Listen to the output and inspect the beginning, cut boundaries, and end. For a downloaded video, run:

```sh
.venv/bin/python -m vedit probe /path/to/downloaded-video.mp4
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
