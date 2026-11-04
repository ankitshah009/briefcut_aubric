# Editor architecture

Briefcut is a general video editor. This is the contract that every part of it builds on.

## One clock

`src/vedit/timeline.py` is the only place that maps source time to finished-video time. Trimming, filler removal, speed, and transitions are all decided before the render, so the compiler, captions, lint, preview, and receipts share one mapping.

- **Source clock:** seconds in the project source. A multi-clip project has one combined source, so plans, transcripts, and tracking still use one clock.
- **Output clock:** seconds in the finished video.
- `resolve_segments(plan)` fills each segment's `keep` from `edit.removed`, `edit.trim_pauses`, and `edit.remove_fillers`.
- `plan_timeline(plan)` returns `{segments, spans, transition_seconds, duration}`.
- `map_cues(cues, spans)` moves captions onto the output clock.

Everything after `select_ranges` is on the output clock, because `select_ranges` applies `keep`, `speed`, and the transition itself. No later step changes timing. Do not add a step that shortens the video after assembly.

## One edit spec

`plan["edit"]` is defined in `src/vedit/edit_spec.py`. `validate_edit` returns a complete bounded dict or raises `EditError`. Media is referenced by asset id, never by path. Catalogues in that file are what Studio and agents see through `GET /api/capabilities` under `editor`.

Per-segment fields beside `start`, `end`, `label`, `reason`, `kind`, `score`:

- `speed`: 0.25 to 4.
- `motion`: optional `{"from": {"scale", "x", "y"}, "to": {"scale", "x", "y"}}`, a two-keyframe move. `scale` is 1 to 3; `x` and `y` are the frame centre as 0 to 1.
- `volume`: 0 to 2, source audio gain.

`keep` is computed and is never accepted from a client.

## Render order

`src/vedit/render_plan.py` turns a validated plan into compile ops. Order is fixed:

1. `reframe` (center path, fused into assembly) unless a cutout needs source geometry.
2. `select_ranges` with `ranges: [{start, end, keep?, speed?, volume?}]` and `transition: {type, seconds}`.
3. `cutout` with a matte on the output clock.
4. `reframe` or `track_reframe`.
5. `stabilize`, `denoise`.
6. `zoom` (motion and zoom modes, per segment, on the output clock).
7. `spatial_effect`.
8. `look`.
9. `voice` (speech cleanup).
10. `text`, `logo`, `overlay` items, on the output clock.
11. `captions` with a provided `.ass` file, always on the output clock.
12. `fade_in`, `fade_out`.
13. `music`, then `loudnorm`.
14. `thumbnail`, `export`.

## Captions

There is one path, in `src/vedit/captions.py`. Speech is held on the source clock in `analysis.transcript` and `analysis.words`. `story_cues` maps it with `map_cues`, runs `polish_cues`, applies `edit.captions.edits`, and `caption_styles.py` writes the `.ass` file that burns. `preview_cues` runs the same path for `timeline.captions`, so Studio shows what will burn. The exception is a translation, which exists only once a render has paid for it, and Meta captions, which are heard at render time.

- **Cue record:** `{start, end, text, key, source_start, words?, estimated?}` on the output clock. `words` are `{start, end, text, estimated?}`.
- **`key`:** what an edit of that line is stored under in `edit.captions.edits`, the cue's start on the source clock in milliseconds, so it survives a change to the cut. An emptied line is removed. An edited line's word times are estimated.
- **Word times:** `whisper-cli --dtw` with its measured lag corrected, or faster-whisper with its lead corrected. Plain token offsets spread words across pauses, and are only the fallback for a build or model that cannot align.
- **Concat clock:** a recognizer that hears only the kept audio, such as Meta Voice Transcribe, gets `extract_kept_audio`: the kept pieces end to end at normal speed. `concat_spans` places what it hears through keep, speed, and transition. A plan with no transcript uses the same clock with the local model.
- **Moments stay apart:** polish never joins cues from different segments.
- **Translation:** `edit.captions.language` burns `meta.translate_cues` output, same cues and same times, and adds `captions.<lang>.srt` beside `captions.srt`. It is refused plainly without Meta. Translated lines have no real word times, so word styles estimate them.

## Multi-clip projects

`request.paths` holds 1 to 30 clips and photos. One video keeps its original file as the source. Anything else is normalized into one project source, `job.source.clips` lists `{name, kind, start, end}`, and clip boundaries count as scene cuts. `src/vedit/project_media.py` decides the canvas and the clip table, and the `project_source` compile op builds the file, one step per item, so `brief.yaml` and `render.sh` in the job's `project-source` folder reproduce it. `CampaignManager._project_source(job)` is the file every later step reads.

## HTTP additions

| Route | Purpose |
| --- | --- |
| `POST /api/campaigns/{id}/timeline` `{plan}` | Resolve a draft without saving. Returns `{ok, timeline: {spans, duration, transition_seconds, captions}}`. |
| `POST /api/campaigns/{id}/assets` `{path}` | Register an uploaded audio or image file. Returns `{ok, asset: {id, kind, name, duration, width, height}}`. `job.assets` maps ids to records. |
| `GET /api/campaigns/{id}/assets?id={asset}` | The stored file of one asset, for the live preview. Only that project's assets folder can answer. |
| `GET /api/campaigns/{id}/filmstrip` | JPEG sprite. `job.source.filmstrip` holds `{url, columns, rows, tile_width, tile_height, count, seconds_per_tile}`. |
| `GET /api/campaigns/{id}/waveform` | `{ok, peaks: [0..1], seconds_per_peak}`. |
| `POST /api/script` `{script, aspect, provider?, budget_usd?, voice?, images?}` | Create a project from written text. Returns `{ok, job}`. Scenes become clips of a normal project, so it edits like any story. `capabilities().script` reports voices, and whether pictures can be generated. |
| `POST /api/campaigns/{id}/background` `{prompt}` | Generate a still with Muse Image in the footage's shape and keep it as an image asset. Needs a Meta project. Returns `{ok, asset}`. |
| `POST /api/campaigns/{id}/find` `{query}` | Find moments by what is seen or said. Returns `{ok, moments: [{start, end, reason, label, source, score}], searched, warnings, budget}`. The transcript search is local and free. Meta also searches the picture on a project made in Meta mode. |
| `POST /api/campaigns/{id}/shorts` `{count?, target_duration?, aspect?, budget_usd?, auto_render?}` | Start 2 to 5 sibling projects with distinct moments. Returns `{ok, jobs}`. `job.family` lists the related projects as `{id, role, index, title, status}`. |

`job.plan.timeline` carries the same object as the `timeline` route so the first paint needs no extra request. `spans` are display data and are recomputed on every save.

## The director's loop

`src/vedit/director.py` holds the settings a note can change, the plain words for each change, and when a loop stops. `apply_patches` in `src/vedit/lint.py` applies patches purely and never raises: a bad patch comes back with `skipped` set.

| Patch | Fields | Effect |
| --- | --- | --- |
| `trim_start`, `trim_end` | `segment`, `seconds` | Move a moment's edge on the source clock. |
| `remove` | `segment` | Drop a moment. |
| `move_before` | `segment`, `seconds` | Move a moment before the moment numbered in `seconds`. |
| `split` | `segment`, `seconds` | Cut a moment in two at a source time. A time inside a sentence moves to that sentence's edge. |
| `set_speed` | `segment`, `value`, `seconds?` | Set a moment's rate. `seconds` picks a half of a split moment. |
| `set_edit` | `path`, `value` | Change one setting from `director.EDIT_FIELDS`. The whole edit must still pass `validate_edit`. |

Every applied record carries `summary`, one plain sentence, which is what "What changed in vN" shows. Lint runs again in strict mode after any model patch.

`request.auto_refine` is 0 to 3 and needs the review on. After a render a person approved, the worker applies the fixes and queues the next version, with source `auto_refine`, until `director.refine_stop` gives a reason. `job.auto_refine` holds `{limit, rounds, active, stopped}`. A round that fails or is cancelled restores the last verified version as current. A note or an applied fix never starts a loop.

## Cutouts

`edit.cutout` removes or treats everything except a subject named in words. It needs Meta mode and the `sam` extra (`uv sync --extra sam`), and says so through `meta.features.cutout` and `editor.features.cutout`.

- **One paid call.** The tracking proxy is assembled by the render's own `select_ranges`, so frame N of SAM's answer is frame N of the finished video. `meta._sam_text` stores the raw response before anything parses it, so tracking and masks for the same subject, and a track that gets refused, never bill twice.
- **Masks are box-local.** A mask fills its record's box, not the frame, at one mask pixel per box pixel. The wire's `x2` and `y2` are inclusive. A frame SAM did not report is held from the nearest mask for up to half a second, never across a cut; after that the subject is gone and only the background shows.
- **Alpha.** The package that reads SAM's masks, `meta_sam_parser`, is under Meta's SAM License, not this project's MIT license, so it stays an optional dependency. `meta.decode_mask` uses only its public decoder, which returns hard 0 or 1 masks; the license bars reverse engineering, so the codec is neither copied nor reached into. The soft edge comes from the matte's own feather.
- **Matte.** `matte.py` streams frames through FFmpeg's stdin into a gray FFV1 file sized to the picture inside the proxy. `atadenoise` calms edge shimmer without touching a hard cut; `tmix` blends the shots either side of a cut. Decoding is pure Python, about 0.1 s of CPU a frame, so it runs in processes.
- **Compositing.** The `cutout` op stays in Y'CbCr, and the tape sets the length whatever the matte does. The `blur` mode is a normalized blur, so the subject's color cannot glow around it.
- **`guided` is not used.** With `guidance=on` this FFmpeg takes the guide on the first pad, the opposite of its documentation, and its 8-bit output wraps instead of clipping. Guarded against both, it still softened every edge we measured compared with bicubic plus the levels curve in `compile._cutout_edge`. That curve steepens as the tape outgrows the matte, so the edge stays a few pixels wide at any size.
- **Proxy shape.** SAM bills by the frame, so an upright clip's tracking proxy is 540x960 rather than a 304x540 picture inside a wide frame. Planning keeps the wide copy. Anamorphic footage is squared by `select_ranges`, and `MediaInfo.square_width` is the width to measure the proxy's picture with.
- **Receipts.** `auto: subject cutout covers the subject` reads coverage and area flicker from the masks that went into the matte, and `report.json` keeps them under `cutout`.

A subject SAM cannot find fails the render with the `center_crop` recovery. Taking it re-renders with tracking off, and a missing subject is then a warning instead.

## Studio editor

`src/vedit/studio/static/timeline.js` holds the editor core, the timeline, and the transcript editor. `preview.js` plays the draft through `timeline.spans` without rendering. Other panels use `window.BriefcutEditor`:

- `draft()`, `job()`, `capabilities()`, `timeline()`, `selection()`, `playhead()`.
- `change(label, fn, options)` is the only way to mutate the draft. It records undo, saves in the browser, and asks `POST /timeline` for the server's spans and captions. Repeats with one label inside a moment become one undo step; pass `{coalesce: false}` for discrete edits.
- `seek(outputSeconds)`, `select(item)`, `undo()`, `redo()`, `play()`, `pause()`.
- `on(event, fn)` for `change`, `job`, `timeline`, `seek`, `select`, and `preview`.
- `api(path, options)` is the Studio request helper. A path that does not start with `/api/` is relative to the current project.

The browser mirrors `plan_timeline` so a drag can repaint on the next frame. `timeline().estimate` is true until the server has answered for the current draft, and the server's answer always replaces the estimate. Captions are shaped only by the server. Between answers the last shape is carried on the source clock.

## Boundaries

- Models return data. `validate_plan`, `validate_edit`, and `apply_patches` decide what is accepted.
- `render.sh` and `brief.yaml` remain a complete record: a delivered edit can be re-run without Briefcut.
- A feature that cannot run on this machine says so through `capabilities()` and is not offered.
- Local mode makes no model calls. Anything sent to Meta needs the existing consent and budget path.
