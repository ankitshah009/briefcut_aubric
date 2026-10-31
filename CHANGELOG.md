# Changelog

## Unreleased

- Studio transfers each selected clip once and reuses it when the edit changes or a render starts. Uploads stream to disk with bounded memory, a timeout, and cleanup of incomplete transfers.
- Automatically allocated session folders are unique even for concurrent requests with the same goal and timestamp. Explicit work directories keep their existing behavior.
- Outdated plan responses are ignored as soon as the edit text changes; failed transfers can be retried without reloading the editor.
- uv is the only supported Python workflow: `scripts/studio.sh` runs `uv sync` when `.venv` is missing, `.python-version` pins 3.13, CI uses `astral-sh/setup-uv` + `uv run pytest`, `uv.lock` is committed, and docs use `uv run vedit ...`.
- Whisper models are discovered under the checkout's `.vedit-models/` (`ggml-base.en.bin` preferred) by the launcher, `vedit doctor`, and captions. The launcher defaults `VEDIT_FFMPEG` to ffmpeg-full when installed.
- Local mode plans from measured signals instead of the first N seconds: `src/vedit/story.py` detects scene cuts (320px downscale), speech ranges, and an optional local transcript, then selects up to eight non-overlapping moments that fit the target, each citing its evidence (`kind`, `score`, reason). Plans carry a bounded `analysis` object (scenes, speech, transcript, notes) that the campaign validator preserves. The summary still states that local mode does not choose a story with AI.
- `vedit story CLIP --goal "..." --target 30 [--json] [--no-transcribe]` prints the same deterministic plan from the command line.
- Meta captions: `caption_provider: "meta"` transcribes the approved speech with `muse-voice-transcribe-1.0` ($0.18 per hour, reserved in the campaign ledger as `transcribe`), writes `captions.srt`/`.ass` on the assembled clock, and burns them locally. Provider failures stop the render; nothing falls back silently. `vedit.meta.check_access()` verifies credentials with the free model listing, Meta uploads request a one-hour server-side expiry, and the `captions` op accepts `file:` for a finished `.srt`/`.ass`.
- Studio shows an analysis-aware source scrubber under the monitor: scene-cut ticks, speech bands, and the selected moments as draggable, resizable blocks (pointer or keyboard; arrows nudge 0.1 s, Shift 1 s). Clicking a moment previews it; clicking again loops it.
- A transcript panel lists local speech lines with jump-to-time, search, and "Add as moment"; lines inside the story are highlighted and the current line follows playback.
- Moments show their kind and strength ("Hook · strong") and the story carries a "Local analysis" or "Meta director" badge with the planner's notes. Local analysis is labeled as deterministic signal analysis, not AI understanding.
- When Meta Voice Transcribe is available, a "Captions by" control chooses Local Whisper or Meta, sent as `caption_provider`.
- The delivery panel adds Run facts: time to story, time to finished video, source → output, planner, caption provider, and Meta spend (server `timings` when present, else browser-measured).
- Keyboard shortcuts (`?` for the list): Space/K play, J/L skip, I/O set in/out at the playhead, 1–8 select a moment, C switches source and finished video. Dark mode follows the system; reduced motion is respected; `#job=<id>` opens a saved project.
- `vedit meta status` and `vedit meta check` report Meta configuration and verify credentials against the free model listing; neither purchases inference.
- Campaign jobs record server-side `timings` (`plan_seconds`, `render_seconds`, `render_revision`), shown in Studio's Run facts and in `scripts/demo_run.py` evidence.
- Hardening from a code review: every `/api/*` route now applies the Host/Origin check (not only campaigns); `/api/run` and `/api/plan` require `Content-Type: application/json` for JSON bodies and never echo render command lines; request bodies read under a socket timeout; `create()` refuses when the worker queue is full instead of persisting an orphaned job.
- Correctness fixes: loudness measurement no longer decodes the video stream (`-vn`), `render.sh` shell-quotes step descriptions, failed or cancelled steps delete partial outputs so `--resume` cannot trust a truncated file, dense tracked `spatial_effect` graphs use the file-backed `-/filter_complex` form, and a transcript with no speech writes a probeable one-cue SRT instead of an empty file that broke the burn.
- The Whisper model under `.vedit-models/` is resolved at call time rather than at import.
- `VEDIT_META_MODE=contributor` is an explicit opt-in that plans stories with `muse-spark-1.3-contributor` at $0.10 / $0.20 per million tokens; capabilities and the ledger report the mode. `scripts/studio.sh` sources an untracked `.env.local` for `MODEL_API_KEY` and these switches.
- When SAM finds no matching subject, the failed job now carries `recovery = {option: "center_crop", message}`; `POST /api/campaigns/{id}/render` accepts `tracking: false` to render that version with a center crop (warning recorded, `tracked_subject` null, subject kept on record, `request.tracking_disabled` persisted so retries honour it) and `tracking: true` re-enables it. Studio shows a "Render with center crop" button in the recovery panel and labels the delivery row "Off (center crop)".
- Director's notes (opt-in `review: true`, checkbox in Meta options): after export verification, Muse Spark reviews a 540p copy of the finished cut and returns a schema-checked verdict, hook score, strengths, timed fixes, and a caption suggestion (`meta.review_cut`, ledger operation `review`). Studio shows the notes beside the delivery with jump-to-time and "Use this caption"; `scripts/demo_run.py --review` records them. A failed review becomes a warning; the verified video is never affected.
- Meta-mode plans are grounded and inspectable: local signal analysis (scene cuts, speech bands, optional local transcript) runs before the Muse Spark call, the transcript is attached to the planning prompt as source-clock data so quotes and speech times are grounded, and the returned plan carries the same `analysis` object, per-segment `kind`/`score` (speech overlap), and scrubber/transcript panel as a local plan. `vedit.story.analyze_signals` and `annotate_segments` are the shared helpers.

- Fixed: burned ASS captions were shifted late because timestamps were written with a millisecond fraction; ASS uses centiseconds (`H:MM:SS.cc`) and libass read `.380` as 3.8 s. Every ASS burn (the Studio default) is now aligned with speech; SRT was unaffected. Commas in caption text are preserved.
- Director's notes (`review: true`): Muse Spark reviews the verified cut and returns a verdict, hook score, strengths, timed fixes, and a caption suggestion; prose is trimmed rather than rejected, and a review failure never fails the verified render.
- Muse Spark calls set `reasoning.effort` (default medium, `VEDIT_META_REASONING` overrides) and allow 16,384 output tokens; an exhausted budget now reports `max_output_tokens` plainly instead of a generic failure.
- Verified live on 2026-10-31: transcript-grounded Spark plans, Meta captions, Muse Image cover, Director's notes, SAM tracking, and the center-crop recovery when SAM finds no subject.

## 1.2.0

- `vedit agent "goal" path.mp4` writes a session: `intent.json`, `inventory.json`, `decisions.md`, `brief.yaml`, `work/project.json`, `work/render.sh`. Render is the default and probes the output. `--plan` stops before execute. Optional flags: `--no-analyze`, `--revise N`, `--work-dir DIR`.
- Critique may revise the brief once into `rev1/`. Exit 0 means planned or verified. Exit 3 means critique failed.
- `vedit tools` is a stdio JSON protocol: `doctor`, `inventory`, `parse_intent`, `decide`, `plan`, `render`, `critique`, `revise`.
- `Vedit.app` opens the editor in its own window and does not open the system browser. `vedit studio` still serves that editor on 127.0.0.1. No account, no cloud. App Sandbox stays off so the window can launch the machine's Python and ffmpeg-full.
- The window shows pauses, shape, captions, and the program as soon as the sentence and the clip are both set. Make the video then plays the probed file.
- Make the video returns when the file is probed. It does not wait on a VMAF pass. Set `VEDIT_VMAF=1` to opt in.
- If the window blocks sound on the first play, the cut still starts muted and says so. Unmute on the player when you want the sound.
- `vedit package dmg --out dist/Vedit.dmg` stages `dist/Vedit.app` and builds a UDZO disk image. The app needs Python 3.13+ and ffmpeg already on the machine. Bundle id `com.ankitshah.vedit`.
- The agent does not call an LLM. The English parser is deterministic. No paid caption or enhance APIs. The brief remains the source of truth.

## 1.1.1

- Plan no longer writes empty `captions.srt` / `captions.ass`. `vedit render DIR` was skipping transcription because those placeholders already existed.
- Resume requires a non-empty output file, not just a path that exists.
- `resolve_font()` finds DejaVu / Liberation / Noto on Linux; CI installs `fonts-dejavu-core`.
- Stream ffmpeg stderr only when stderr is a TTY (pytest capture was deadlocking).

## 1.1.0

- Auto VideoToolbox on Darwin for delivery presets; `web_preview` stays libx264. `--hw` / `--no-hw` / `hw: auto`.
- Hardware encode falls back to libx264 if VideoToolbox fails.
- YouTube / Shorts / mezzanine presets tag bt709 + tv range + high profile + faststart.
- `vedit render DIR` resumes a planned `project.json` work directory.
- Every plan writes `timeline.otio.json` (OTIO-lite interchange).
- Execute prints `[n/total]` and streams ffmpeg stderr so long encodes are not a black box.
- Master encode and silence-cut write `.part` then rename (crash-safe).
- Verify reports color tags. Probe exposes primaries / trc / space / range.
- Spoken-caption proof against whisper-cpp `jfk.wav` (skipped when the fixture is absent).
- GitHub Actions CI on Python 3.13 + apt ffmpeg.

## 1.0.0

- Agent-native local NLE compiler: brief → project.json / render.sh → FFmpeg.
- Deferred silence-cut, captions, two-pass loudnorm. YouTube path is two encodes + verify.
