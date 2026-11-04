# Editor architecture

Briefcut is a general video editor. This page is the detailed contract that every part of it builds on: the fields, routes, patch ops, and rules. For the system diagrams and the reasons behind them, read [ARCHITECTURE.md](../ARCHITECTURE.md) first. For the why, read [WHY.md](WHY.md).

## One clock

`src/vedit/timeline.py` is the only place that maps source time to finished-video time. Trimming, filler removal, speed, and transitions are all decided before the render, so the compiler, captions, lint, preview, and receipts share one mapping. The diagram is [One clock](../ARCHITECTURE.md#4-one-clock).

- **Source clock:** seconds in the project source. A multi-clip project has one combined source, so plans, transcripts, and tracking still use one clock.
- **Output clock:** seconds in the finished video.
- `resolve_segments(plan)` fills each segment's `keep` from `edit.removed`, `edit.trim_pauses`, and `edit.remove_fillers`.
- `plan_timeline(plan)` returns `{segments, spans, transition_seconds, duration}`.
- `map_cues(cues, spans)` moves captions onto the output clock.

Everything after `select_ranges` is on the output clock, because `select_ranges` applies `keep`, `speed`, and the transition itself. No later step changes timing. Do not add a step that shortens the video after assembly.

## One edit spec

`plan["edit"]` is defined in `src/vedit/edit_spec.py`. `validate_edit` returns a complete bounded dict or raises `EditError`. Unknown keys are dropped. Media is referenced by asset id, never by path. Catalogues in that file are what Studio and agents see through `GET /api/capabilities` under `editor`.

Per-segment fields beside `start`, `end`, `label`, `reason`, `kind`, `score`:

- `speed`: 0.25 to 4.
- `motion`: optional `{"from": {"scale", "x", "y"}, "to": {"scale", "x", "y"}}`, a two-keyframe move. `scale` is 1 to 3; `x` and `y` are the frame centre as 0 to 1.
- `volume`: 0 to 2, source audio gain.

`keep` is computed and is never accepted from a client. `validate_plan` in `campaign.py` also takes `analysis` and `provider` from the stored plan, never from the client.

Edit fields that Meta models propose, each with its own validator:

- `edit.hero_word` (`validate_hero_word`): `{word, start, end, subject, side, font, color, reason}`, plus `subject_box` once SAM has masked the window. At most 2 words and 18 characters, 0.5 to 3 s, on the output clock.
- `edit.motion_card` (`validate_motion_card`): `{kind, text, subtitle, style, palette, camera, duration, reason, backdrop?}`. `kind` is `intro_title`, `end_card`, or `lower_third`. Every choice comes from a closed list. `backdrop` is an image asset id.
- `edit.captions.emphasis` (`validate_emphasis`): `[{cue_key, word_index, word}]`, at most 2 a line and 240 a story.

## Render order

`src/vedit/render_plan.py` turns a validated plan into compile ops. `STAGES` fixes the order:

1. `reframe_center`: the centre path, fused into assembly, unless a layer needs the source geometry.
2. `assembly`: `select_ranges` with `ranges: [{start, end, keep?, speed?, volume?}]` and `transition: {type, seconds}`.
3. `cutout`, with a matte on the output clock.
4. `hero_word`, on the assembled picture before any reframe, so its matte lands on the source geometry.
5. `reframe_subject`: `reframe` or `track_reframe`.
6. `stabilize`, then `denoise`.
7. `zoom` (motion and zoom modes, per segment, on the output clock).
8. `spatial_effect`.
9. `look`.
10. `voice` (speech cleanup).
11. `texts`, `logo`, `overlays`, on the output clock.
12. `motion_card`, under the captions and the fades.
13. `captions`, with a provided `.ass` file, always on the output clock. They are the accessibility layer, so they draw over the motion card and stay readable while a title plays.
14. `fades`.
15. `music`, then `loudness`.
16. `thumbnail`, then `export`.

Each stage emits ops that `compile.py` looks up in `OPS`. An unknown op is a `CompileError`.

## Captions

There is one path, in `src/vedit/captions.py`. Meta Voice Transcribe is the only speech model. Speech is held on the source clock in `analysis.transcript`. `story_cues` maps it with `map_cues`, runs `polish_cues`, applies `edit.captions.edits`, and `caption_styles.py` writes the `.ass` file that burns. `preview_cues` runs the same path for `timeline.captions`, so Studio shows what will burn. A translation is the exception: it exists only once a render has paid for it.

- **Cue record:** `{start, end, text, key, source_start, words?, estimated?}` on the output clock. `words` are `{start, end, text, estimated?}`.
- **`key`:** what an edit of that line is stored under in `edit.captions.edits`, the cue's start on the source clock in milliseconds, so it survives a change to the cut. An emptied line is removed. An edited line's word times are estimated.
- **Word times:** Voice Transcribe reports turns, not words. `interpolate_words` spreads a turn's words across its time by character length and marks them `estimated`, which is enough to drive a word highlight. Filler removal needs word times, so it has little to cut at.
- **Heard once:** planning hears the whole project once (`CampaignManager._hear`). A render cuts its captions from that transcript at no cost. A short reuses its parent's transcript.
- **Concat clock:** when planning heard nothing, `_meta_captions` sends Voice Transcribe only the kept audio, made by `extract_kept_audio`: the kept pieces end to end at normal speed. `concat_spans` places what it hears through keep, speed, and transition. An edit that does not match a heard line is reported, not silently dropped.
- **Moments stay apart:** polish never joins cues from different segments.
- **Translation:** `edit.captions.language` burns `meta.translate_cues` output, same cues and same times, and adds `captions.<lang>.srt` beside `captions.srt`. It runs on Muse Spark, or on Muse Glimmer on this machine in private mode. Translated lines have no real word times, so word styles estimate them, and punch words are not drawn on them.
- **Punch words:** with `edit.captions.emphasize`, the render asks Muse Spark for each line's punch word (`emphasis.suggest_emphasis`). Only words a line really has, at that index, are kept.

## Multi-clip projects

`request.paths` holds 1 to 30 clips and photos. One video keeps its original file as the source. Anything else is normalized into one project source, `job.source.clips` lists `{name, kind, start, end}`, and clip boundaries count as scene cuts. `src/vedit/project_media.py` decides the canvas and the clip table, and the `project_source` compile op builds the file, one step per item, so `brief.yaml` and `render.sh` in the job's `project-source` folder reproduce it. `CampaignManager._project_source(job)` is the file every later step reads.

## HTTP routes

Studio listens on 127.0.0.1 only. New projects go through `campaign.product_request`, which fills in Meta and refuses `provider="local"`.

| Route | Purpose |
| --- | --- |
| `GET /api/capabilities` | What this machine and Meta can do, without a network call. |
| `POST /api/upload?name=` | Store an upload in the local inbox. Nothing is sent anywhere. |
| `GET /api/campaigns`, `GET /api/campaigns/{id}` | The recent projects, or one project with its plan, result, versions, and budget. |
| `POST /api/campaigns` | Create a project: `product_request`, then `CampaignManager.create`. Returns `202 {ok, job}`. |
| `POST /api/campaigns/{id}/timeline` `{plan}` | Resolve a draft without saving. Returns `{ok, timeline: {spans, duration, transition_seconds, captions}}`. |
| `POST /api/campaigns/{id}/render` `{plan, director?, captions?, aspect?, auto_refine?, tracking?}` | Approve and queue a render as the next revision. |
| `POST /api/campaigns/{id}/revise` `{mode, note?, director?}` | `mode` is `director`, `auto`, or `note`. Renders version N+1 and keeps version N. |
| `POST /api/campaigns/{id}/retry`, `/cancel` | Retry a failed or cancelled job, or cancel one. |
| `GET /api/campaigns/{id}/artifact?name=&v=` | `source`, `video`, `cover`, `bundle`, `brief`, or `report`, optionally of verified revision `v`. |
| `POST /api/campaigns/{id}/assets` `{path}` | Register an uploaded audio or image file. Returns `{ok, asset: {id, kind, name, duration, width, height}}`. `job.assets` maps ids to records. |
| `GET /api/campaigns/{id}/assets?id={asset}` | The stored file of one asset, for the live preview. Only that project's assets folder can answer. |
| `GET /api/campaigns/{id}/filmstrip` | JPEG sprite. `job.source.filmstrip` holds `{url, columns, rows, tile_width, tile_height, count, seconds_per_tile}`. |
| `GET /api/campaigns/{id}/waveform` | `{ok, peaks: [0..1], seconds_per_peak}`. |
| `POST /api/script` `{script, aspect, language?, budget_usd?, images?}` | Create a project from written text, through `product_request`. Scenes become clips of a normal project, so it edits like any story. Meta has no text-to-speech, so script videos are caption-led and a `voice` is refused. |
| `POST /api/campaigns/{id}/background` `{prompt}` | Generate a still with Muse Image in the footage's shape and keep it as an image asset. Returns `{ok, asset}`. |
| `POST /api/campaigns/{id}/find` `{query, verify?, drop_unseen?}` | Find moments by what is said (local, free) and what is seen (Muse Spark). With `verify` (the default), SAM 3.1 checks each moment Spark found. Returns `{ok, moments, searched, warnings, budget, verification}`. Each moment carries `verified`, `coverage`, `flagged`, and `thumbnail_url`. |
| `GET /api/campaigns/{id}/find-thumbnail?name=` | SAM's picture of a found moment, by name, from this project's folder only. |
| `POST /api/campaigns/{id}/hero-word` `{subject?, font?, color?}` | A checked `edit.hero_word` suggestion from Muse Spark. Nothing is saved or rendered. |
| `POST /api/campaigns/{id}/motion-card` `{kind?, backdrop?}` | A checked `edit.motion_card` from Muse Spark, with an optional Muse Image backdrop kept as an asset. The plan is not changed. |
| `POST /api/campaigns/{id}/shorts` `{count?, target_duration?, aspect?, budget_usd?, auto_render?}` | Start 2 to 5 sibling projects with distinct moments. Returns `{ok, jobs}`. `job.family` lists the related projects as `{id, role, index, title, status}`. |
| `GET /api/campaigns/{id}/meta-calls` | The project's Meta call log from `meta.call_log`: purposes, models, sizes, costs, response ids, and `runs_on`. Never keys, prompts, transcripts, or media. |

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

Every applied record carries `summary`, one plain sentence, which is what "What changed in vN" shows. `CampaignManager._patched` runs the lint again in strict mode after any model patch and applies its fixes last.

The director is `spark` (Muse Spark, the default) or `wonder` (Muse Wonder, which also listens). The choice comes from the request's `director`, else `VEDIT_META_DIRECTOR`, else Spark. A Wonder review reserves its full output allowance at Wonder's prices, so it needs a budget of about $0.40; a lower budget is refused before the upload, with the amount to raise it to.

`request.auto_refine` is 0 to 3 and needs the review on. After a render a person approved, the worker applies the fixes and queues the next version, with source `auto_refine`, until `director.refine_stop` gives a reason. `job.auto_refine` holds `{limit, rounds, active, stopped}`. A round that fails or is cancelled restores the last verified version as current. A note or an applied fix never starts a loop. A round that would lead back to a cut already made stops the loop.

## Receipts

Every check is `{name, passed, at, note, basis, by, inputs?}`. `basis` is `observed` (read from the delivered file), `derived` (code measured stored inputs), or `inferred` (a model's judgment). `by` names the instrument, and `inputs` names any model whose data a measurement read. `at` is on the output clock. `CampaignManager._grade` merges the lint checks, the requested-edits check, the SAM checks (`subject_in_frame_check`, `subject_cutout_check`, `hero_word_check`), and the director's checks, which are always `inferred`. The diagram is [Receipts and evidence labels](../ARCHITECTURE.md#5-receipts-and-evidence-labels).

## Cutouts

`edit.cutout` removes or treats everything except a subject named in words. It needs a Meta project and the `sam` extra (`uv sync --extra sam`), and says so through `meta.features.cutout` and `editor.features.cutout`.

- **One paid call.** The tracking proxy is assembled by the render's own `select_ranges`, so frame N of SAM's answer is frame N of the finished video. `meta._sam_text` stores the raw response before anything parses it, so tracking and masks for the same subject, and a track that gets refused, never bill twice.
- **Masks are box-local.** A mask fills its record's box, not the frame, at one mask pixel per box pixel. The wire's `x2` and `y2` are inclusive. A frame SAM did not report is held from the nearest mask for up to half a second, never across a cut; after that the subject is gone and only the background shows.
- **Alpha.** The package that reads SAM's masks, `meta_sam_parser`, is under Meta's SAM License, not this project's AGPL-3.0 license, so it stays an optional dependency. `meta.decode_mask` uses only its public decoder, which returns hard 0 or 1 masks; the license bars reverse engineering, so the codec is neither copied nor reached into. The soft edge comes from the matte's own feather.
- **Matte.** `matte.py` streams frames through FFmpeg's stdin into a gray FFV1 file sized to the picture inside the proxy. `atadenoise` calms edge shimmer without touching a hard cut; `tmix` blends the shots either side of a cut. Decoding is pure Python, about 0.1 s of CPU a frame, so it runs in processes.
- **Compositing.** The `cutout` op stays in Y'CbCr, and the tape sets the length whatever the matte does. The `blur` mode is a normalized blur, so the subject's color cannot glow around it.
- **`guided` is not used.** With `guidance=on` this FFmpeg takes the guide on the first pad, the opposite of its documentation, and its 8-bit output wraps instead of clipping. Guarded against both, it still softened every edge we measured compared with bicubic plus the levels curve in `compile._cutout_edge`. That curve steepens as the tape outgrows the matte, so the edge stays a few pixels wide at any size.
- **Proxy shape.** SAM bills by the frame, so an upright clip's tracking proxy is 540x960 rather than a 304x540 picture inside a wide frame. Planning keeps the wide copy. Anamorphic footage is squared by `select_ranges`, and `MediaInfo.square_width` is the width to measure the proxy's picture with.
- **Receipts.** `auto: subject cutout covers the subject` reads coverage and area flicker from the masks that went into the matte, and `report.json` keeps them under `cutout`.

A subject SAM cannot find fails the render with the `center_crop` recovery. Taking it re-renders with tracking off, and a missing subject is then a warning instead.

The hero word uses the same masks over its window only (`hero_word.window_matte`), and verified find uses them to check Spark's moments (`verify_find.verify_moments`). Both are drawn in [Feature pipelines](../ARCHITECTURE.md#8-feature-pipelines).

## Studio editor

`src/vedit/studio/static/timeline.js` holds the editor core, the timeline, and the transcript editor. `preview.js` plays the draft through `timeline.spans` without rendering. `meta-calls.js` is the Meta calls panel. Other panels use `window.BriefcutEditor`:

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
- Every product entry point is Meta-only through `product_request`. The manager's model-free path exists for the test suite. Anything sent to Meta needs the consent box (or an agent's explicit call), the project's budget, and a reservation in `meta._reserve` first.
