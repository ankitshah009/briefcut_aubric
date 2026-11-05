# Briefcut by Aubric, Inc.: hackathon submission

**Demo video.** <https://youtu.be/_VNGaeO-WIw>

**One line.** Briefcut is an open-source (AGPL-3.0-or-later) video editor that turns clips and photos into an editable story and a verified, downloadable campaign, for creators, small teams, and AI agents. Meta's models propose, track, transcribe, and critique; FFmpeg on the creator's own computer is the only thing that cuts; every version ships with receipts anyone can verify. It costs cents per project on the user's own Meta key, with no subscription.

Event: Meta Global AI Developer Hackathon. Submission deadline **November 4, 2026, 9:00 PM ET**. Official event page: <https://dev.meta.ai/events/global-hackathon>. This document is the write-up. Every claim here is meant to stay narrower than the evidence file linked beside it.

## For judges

- **Setup.** The [README quickstart](../README.md#judge-quickstart) runs from a clean clone: uv, FFmpeg, `cp .env.example .env.local`, your own key from <https://dev.meta.ai>, then `bash scripts/studio.sh --open`. The repository contains no key. A fresh clone was run through it on 2026-11-04 ([scenario matrix, run C](evidence/scenario-matrix/README.md)).
- **Which Meta models, and where.** The table in the [README](../README.md#which-meta-models-briefcut-uses-and-where-each-runs) and [below](#how-metas-models-are-used). Five run on the Meta Model API, called from the judge's computer. Muse Glimmer runs on the judge's computer in private mode.
- **Why it is built this way.** [WHY.md](WHY.md) gives the reasoning. [ARCHITECTURE.md](../ARCHITECTURE.md) has the diagrams: one project end to end, how a model's answer becomes FFmpeg steps without running as code, the one clock, receipts, Meta spend, each feature pipeline, and the agent surface.
- **No key?** The [recorded runs](../README.md#no-key-see-recorded-runs) in `docs/evidence/` hold each run's request, what came back, the probe of the delivered file, the receipts, and the spend, with Meta's response ids.
- **Real footage.** The [scenario matrix](evidence/scenario-matrix/README.md) has 14 renders on footage Briefcut was not built around, and the 11 bugs it found. Their status is [below](#what-the-scenario-matrix-found).
- **What is sent to Meta.** [PRIVACY.md](PRIVACY.md).
- **Who it is for, and why it grows Meta's models.** [Impact](#impact).

## The problem

Creators and small makers have footage and no time. A phone clip of a product being made sits unused because turning it into a short vertical story means scrubbing, choosing moments, cutting on sentences, reframing, captioning, exporting, and checking the file. "AI editors" that do this today are black boxes: you cannot see why a cut was chosen, you cannot re-run the result, you cannot tell the model it was wrong without starting over, and you cannot prove the exported file is what you approved.

## Impact

- **Who benefits.** Creators with phone footage and no time; small businesses and teams making product or process videos; educators and podcasters cutting recordings into clips (a project holds up to 2 hours, and long-form planning works across all of it); developers and agents that need video as an API; and anyone with private footage. See [Who it is for](../README.md#who-it-is-for).
- **Why they would pick it over CapCut, Descript, Opus Clip or Premiere.** Every claim can be checked (`vedit verify`), any agent can drive it over MCP, originals stay on the computer, it is open source under AGPL-3.0-or-later, and it is pay per use at $0.0005 to $0.08 per project in the [scenario matrix](evidence/scenario-matrix/README.md). The others still lead on templates, stock media, mobile apps, collaboration, generated video, and publishing. The table and the honest list are in the [README](../README.md#why-briefcut-instead-of-capcut-descript-opus-clip-or-premiere).
- **Why it grows Meta's model adoption.** Each project is real usage of Meta's hosted models, five of them in all, at cents a project. One MCP server makes any agent, Muse Code first, a video editor on Meta's models. Receipts give teams an audit trail for model-made edits. The open-source code (AGPL-3.0-or-later) is a reference for Meta's models working together (Spark proposes, SAM 3.1 checks, Wonder listens), private mode puts Meta's open weights to work on a laptop, and Voice Transcribe hears 25 languages. Each point, with its evidence, is in the [README](../README.md#why-this-grows-metas-model-adoption).
- **Why long videos work.** By Meta's docs, Muse Spark and Muse Wonder have a 1,048,576-token context window, and a two-hour talk's transcript is about 2% of it (an estimate), so long-form planning watches in windows of 10 minutes or less and then picks the story in one call over the whole recording, with code checking every pick. Live, it cut Tears of Steel, a 12-minute film, into a 44 s trailer and three shorts for $0.34; the editor call used 0.31% of the context ([evidence](evidence/long-form/README.md), [README](../README.md#long-videos-why-a-long-view-matters)).

## What Briefcut does

1. **Bring in your footage**: up to 30 videos and photos, up to 4 GB each. Planning with Meta takes up to 2 hours of footage; past 10 minutes it runs in parts, and Briefcut states the expected cost before it encodes or pays for anything. The original never leaves the machine. Meta receives a reduced copy, an audio track, or text, after the creator confirms.
2. **Describe the outcome** in a sentence: "Open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end." Editing phrases in the brief become engine ops (`src/vedit/brief_ops.py`): pause trimming, a timed title card, fade in and out, denoise, keep original loudness, `hook:` and `cta:` overlays. Each applied or ignored instruction is listed as a decision on the plan.
3. **Get an editable story plan.** Meta Voice Transcribe hears the project once. Muse Spark watches the footage and proposes the moments, with that transcript and its sentence boundaries offered as preferred cut points. Deterministic code then snaps every cut to a sentence boundary (`snap_to_sentences` in `src/vedit/story.py`). Each moment carries a reason and a source time.
4. **Correct it**: move, trim, reorder, add, or delete moments against the source video; edit title and post copy; add a spatial effect, a hero word behind the subject, punch words in the captions, or a 3D motion card.
5. **Approve the render.** The approved plan compiles to a YAML brief → `project.json` → `render.sh` → FFmpeg. The job is durable: closing the browser does not stop it.
6. **Verify and read the receipts.** The server probes the delivered file (duration, dimensions, streams, bt709/tv color) and refuses to mark the job complete otherwise. Then `src/vedit/lint.py` grades the cut (cuts on sentence boundaries, duration vs target, moments inside the source and in order, caption coverage, requested edits applied). With review on, Muse Spark, or Muse Wonder if chosen, adds its own checks. Each check is labeled Probed, Measured, or Model opinion, and each failing check comes with a structured patch.
7. **Revise without starting over**: "Apply director's fixes", "Apply automatic fixes", or a typed director's note render version N+1. Version N stays downloadable with its own report.
8. **Download**: video, cover, post copy, captions, `story.json`, `brief.yaml`, `report.json`.

Every Meta call shows in Studio's Meta calls panel as it happens.

## What makes it different

The model proposes and critiques; it never executes. Muse Spark returns data (ranges, checks, patches), SAM returns geometry, Voice Transcribe returns cues. The compiler is the only thing that produces executable steps, and a deterministic lint runs after every model patch. A cut left inside a sentence moves to the nearest sentence boundary before anything renders, and anything the lint cannot fix appears as a failed check. Meta's models also check each other: SAM 3.1 looks for what Muse Spark found, and the hero word's side is chosen from SAM's mask, not from Spark's guess. Every version ships with receipts: the probe of the file, the list of checks that passed and failed with a jump-to-time, and the exact patches that produced it from the previous version.

## Where it sits next to CapCut

CapCut is the consumer standard for short-form editing. Briefcut's bet is the part its pages do not describe: the model reviews its own render and proposes structured fixes, each version carries pass or fail receipts, and the edit can be re-run as a program. Feature-by-feature status, with sources, is in [CAPCUT_PARITY.md](CAPCUT_PARITY.md).

| | CapCut, from its own pages | Briefcut |
| --- | --- | --- |
| How the cut is chosen | AutoCut proposes an edit from speech analysis, which you refine | Every moment carries a reason and a source timestamp; cuts land on transcript sentences; you edit the plan before anything renders |
| Fixing a bad result | Not described beyond editing by hand | Muse Spark or Muse Wonder reviews the render and returns structured patches; version N+1 renders and version N stays verified |
| Proof the file is right | Not described | A probe of the delivered file plus pass or fail receipts with jump-to-time, per version |
| Re-running an edit | Not described | `render.sh` re-runs the edit with FFmpeg alone; `brief.yaml` recompiles it |
| Where footage goes | The pages checked link to a privacy policy and do not say | The original stays on the machine. Meta receives a reduced copy, audio, or text after the creator confirms, and Studio lists each Meta call. Private mode keeps three text steps on the machine |
| Agent access | A "CapCut × Codex" page describes natural-language instructions producing an editable rough cut; no public API documentation was found | HTTP API and an MCP server with 22 tools that any MCP agent can use, including Meta's Muse Code |

## How Meta's models are used

Every model Briefcut calls is a Meta model. All calls live in one adapter, `src/vedit/meta.py`, behind `MODEL_API_KEY` and `VEDIT_META_ENABLED=1` (or the same two settings in Studio). They go from the user's computer to the Meta Model API (`https://api.meta.ai/v1`), except the three private-mode steps, which go to Muse Glimmer on the same computer. Models return **data**, never commands; every result is schema-validated before the compiler sees it. Each project carries a spending cap. The adapter reserves an upper bound before each call, including an allowance for the video that Meta's token count leaves out, and keeps the reservation after an uncertain failure. Prices below are as encoded in `meta.py` from <https://dev.meta.ai/docs/pricing-rate-limits> on November 4, 2026.

FFmpeg renders on the user's computer and is not a model. Nor is Blender, which renders motion cards from a fixed template. Sentence snapping, lint checks, the patcher, and the compiler are deterministic code.

| Capability | Model | Where in the code | What it does in Briefcut |
| --- | --- | --- | --- |
| Transcript and captions | `muse-voice-transcribe-1.0` (`POST /v1/asr/transcribe`) | `meta.transcribe_audio`, called through `captions.transcribe` from `CampaignManager._hear` | The only transcriber. It hears the project once at planning, as mono 16 kHz WAV with silence left out (at most 10 minutes and 32 MB per request). The plan, the lint, find, and the captions all read that transcript. It returns turn-level cues, so word times are estimated by length and filler removal has nothing to cut at. `polish_cues` in `src/vedit/captions.py` collapses doubled words and repeated phrases, merges fragments into sentences, capitalises, and wraps to two balanced lines of at most 42 characters. Captions burn in locally. $0.18 per hour of audio. |
| Story planning | `muse-spark-1.3`, or `muse-spark-1.3-contributor` with `VEDIT_META_MODE=contributor` | `meta.plan_story`, called from `CampaignManager._plan` in `src/vedit/campaign.py` | Watches a 960×540 analysis proxy (540×960 for upright footage) of the full project, with the transcript and its sentence boundaries attached as source-clock data, and returns 1 to 8 non-overlapping ranges with visible reasons, title, summary, caption, hashtags, and a cover prompt. $1.25 / $4.25 per million tokens (contributor $0.10 / $0.20). |
| Director's notes and checks | same Spark model by default | `meta.review_cut`, called after export verification when `review: true` | Reviews a 540p copy of the finished cut and returns a verdict, hook score, strengths, timed fixes, a caption suggestion, structured `checks`, and structured `patches` (`trim_start`, `trim_end`, `remove`, `move_before`, `split`, `set_speed`, `set_edit`). A failed review becomes a warning; the verified file is unchanged. |
| Director who listens | `muse-wonder-1.0` | `meta.review_cut` with `director="wonder"`, or `VEDIT_META_DIRECTOR=wonder` | The same review, with audio checks: captions against the voice, level across cuts, pops and gaps. Standard mode only, $4 / $20 per million tokens. A review reserves $0.33 before the video is counted, so it needs a project budget of at least that; below it Briefcut refuses and uploads nothing. |
| Director's note to patches | same Spark model, text only, or Muse Glimmer in private mode | `meta.patch_plan`, called from `CampaignManager.revise` with `mode: "note"` | Turns a typed note into the same patch schema. Nothing is uploaded; the outline and sentence list go as text. |
| Find a moment, verified | Spark, then `sam-3.1` | `meta.find_moments`, then `verify_find.verify_moments` | Spark finds where something appears in the footage. SAM 3.1 then masks the named thing in a 3 s window of each of up to 5 moments (about $0.01 each) and marks each moment verified or flagged. A flagged moment is kept and ranked last unless the caller asks to drop it. Searching what was said runs over the transcript on the user's computer. |
| Long video to shorts | same Spark model | `meta.plan_shorts` | Plans 2 to 5 distinct shorts from one source as sibling projects. |
| Script to video | Spark (or Glimmer in private mode), then `muse-image-1.0` | `meta.script_prompts`, `meta.generate_image` | Spark turns the scenes of a written script into picture prompts; Muse Image makes one still per scene. The video is caption-led, with the script's words timed evenly across each scene. Nothing narrates it, because Meta has no text-to-speech model. |
| Caption translation | same Spark model, text only, or Glimmer in private mode | `meta.translate_cues` | Translates caption text cue by cue; the cue timing is kept. |
| Punch words | same Spark model, text only | `emphasis.suggest_emphasis` | Picks the word that carries a caption line, on at most 40% of lines. Briefcut checks each pick names a real line and a word that line has, and the caption styles draw it larger and in an accent color. |
| Hero word | same Spark model, then `sam-3.1` | `hero_word.py`, compiled by the `hero_word` op | Spark reads the cut's caption words and proposes one spoken word and a window. Code checks the word is said there, inside one shot, for 0.5 to 3 s. SAM 3.1 masks the subject over that window only, the side is chosen from the mask, and the compiler layers background, word, then subject. |
| 3D motion card | same Spark model, and `muse-image-1.0` for the backdrop | `blender_cards.py`, compiled by the `motion_card` op | Spark picks the card's words, style, palette, camera, and length from closed lists; Muse Image paints an optional backdrop. Blender (optional, 4.2 or later) renders Briefcut's own template headless on this computer, and the compiler lays the card over the opening or closing seconds without changing the clock. |
| Subject tracking | `sam-3.1` (video segmentation) | `meta.track_subject`; geometry mapped by `remap_proxy_points`; applied by `_track_reframe` in `src/vedit/compile.py` | Follows the named subject across the approved cut only, returns per-frame boxes, and drives a smooth 9:16 reframe of the original footage. $0.20 per 1,000 video frames, 50-frame minimum, at most 15,000 frames. When nothing matches, the job offers a center-crop recovery instead of failing silently. |
| Cutout | `sam-3.1` | `meta.subject_masks`, decoded with the public `meta-sam-parser` decoder; applied by `_cutout` in `src/vedit/compile.py` | Lossless per-frame masks of the named subject. The cutout keeps the subject over a blurred, darkened, black-and-white, solid-color, or new image background. |
| Cover image | `muse-image-1.0` (image edit) | `meta.generate_cover` | Edits a real frame from the delivered video into a PNG cover; opt-in per project. $0.01 per image. |
| Generated background | `muse-image-1.0` | `meta.generate_background` | Makes a background from a description, for the cutout. Needs `consent: true`. $0.01 per image. |

Nothing is sent to Meta by opening Studio. A Meta request happens only after the creator creates a story and ticks "Send this footage to Meta using my API key". Studio's Meta calls panel lists each call with its purpose, status, model, cost, duration, size, and Meta's response id. It reads the project's `meta-calls.jsonl` through `GET /api/campaigns/{id}/meta-calls`.

### Private mode

Private mode runs the director's note, caption translation, and the script scene writer on Muse Glimmer 30B (`meta-models/Muse-Glimmer-30B`, Apache 2.0, distilled from Muse Spark), on the user's computer through llama.cpp (`src/vedit/open_weights.py`). Turn it on with `VEDIT_PRIVATE_MODEL=glimmer` or the Settings toggle, after `uv run python -m vedit.open_weights serve`. The server binds `127.0.0.1:8091`, needs a key from a 0600 file, and refuses weights whose sha256 does not match the official GGUF. Private mode fails closed and never falls back to the Meta API. The callers keep their prompts, schemas, validation, and caches; only the model call moves. Structured edits use temperature 0.3, and an incomplete patch gets one second try. Story planning from video, the review, SAM 3.1, Muse Image, and Voice Transcribe stay on the Meta Model API.

## Architecture

[ARCHITECTURE.md](../ARCHITECTURE.md) has the full set of diagrams. In short:

```text
browser · Mac app · terminal · MCP agents (Muse Code, Claude Code, ...)
   ▼
Studio HTTP API (studio/server.py) · CLI (cli.py) · MCP over stdio (mcp_server.py)
   ▼
product_request: the Meta-only gate
   ▼
CampaignManager (src/vedit/campaign.py)  SQLite job record · one worker · cancel · retry · versions
   ├── Meta adapter (src/vedit/meta.py)          → Meta Model API: Spark, Wonder, SAM 3.1, Voice Transcribe, Muse Image
   │        spending cap · schema validation · upload cleanup · call log      → private mode: Muse Glimmer via llama.cpp, this machine
   ├── Story (src/vedit/story.py)                scene cuts, speech ranges, sentences; snap_to_sentences
   ├── Brief ops (src/vedit/brief_ops.py)        "trim the pauses", "title card", "fade out" → ops + decisions
   ├── Lint (src/vedit/lint.py)                  deterministic checks + patches; apply_patches is pure
   ▼
approved plan → YAML brief → compiler (src/vedit/compile.py) → project.json + render.sh
   ▼
FFmpeg (and Blender for motion cards) → probe → report.json → checks → campaign.zip
   ▼
POST /api/campaigns/{id}/revise  (director | auto | note) → patches → relint → version N+1
```

The boundary that matters: the model proposes moments, geometry, cues, and patches; the compiler is the only thing that produces executable steps. A judge can open `brief.yaml` from any delivery, and `render.sh` from its project folder, and re-run the edit.

### Agents can drive it

`vedit mcp` exposes the same project flow over MCP (`src/vedit/mcp_server.py`) with 22 tools, so any MCP agent can use it: import clips, create a story, wait, render, edit, revise with a mode or a note, find moments with SAM verification, make shorts, turn a script into a video, propose a hero word or a motion card, add assets, generate a background, read the receipts, verify a delivery, and resolve artifact paths. The repository ships a project `.mcp.json`, a Claude Code plugin, and the skill at `.agents/skills/briefcut/` for Muse Code. All routes go through the one `CampaignManager`, so an agent gets the same verification and version history a person sees. See "MCP for coding agents" in `docs/CAMPAIGN.md` and [MUSE_CODE.md](MUSE_CODE.md).

## Live evidence (2026-11-04, Meta-only)

These runs used the current code paths. All Muse Spark calls used contributor mode, which permits Meta to train on what it received. Only demo clips and public samples were sent. Spend is Briefcut's estimate from Meta's reported usage, not an invoice.

| Flow | Evidence and result | Spend |
| --- | --- | ---: |
| Scenario matrix | [`scenario-matrix/README.md`](evidence/scenario-matrix/README.md): 14 renders. 6 on public sample clips with live Meta through the exact `.mcp.json` command, 5 on real iPhone clips and photos with Meta stubbed and sockets blocked, and 3 on the demo clip, one from a fresh clone following the README. Every render's probe matched an independent ffprobe except one (bug 9). | $0.1069 (16 calls) |
| Private mode | [`open-weights/README.md`](evidence/open-weights/README.md): Muse Glimmer 30B on an Apple M5 Max handled two director's notes, English to Spanish and to Hindi translation, and three script scenes, with the Meta API call replaced by a function that fails the run. The hardest note got both edits in 6 of 8 runs at temperature 0.3. Studio's Meta calls panel showed the two private calls beside five Meta API calls of the same project. | $0 |
| Muse Wonder as director | [`wonder-review/README.md`](evidence/wonder-review/README.md): one 18.9 s cut, two reviewers. Wonder failed two checks that the frames confirm and proposed two patches; Spark passed everything. With no captions on screen, both models heard all six sentences, but only Wonder described the soundtrack truthfully; Spark invented a lathe sound. | $0.1366 (4 calls) |
| Verified find | [`verified-find/README.md`](evidence/verified-find/README.md): Spark found the bowl and SAM verified it in 100% of sampled frames. Spark found the gouge correctly, but SAM did not recognise the stylised gouge, so it was flagged, not dropped. "A dog" found nothing. | $0.0740 |
| Hero word | [`hero-word/README.md`](evidence/hero-word/README.md): Spark picked "oak"; SAM masked 60 of 60 frames; the mask overruled Spark's "center" (82% covered) for "top" (20%). Two renders, 16:9 and 9:16, probed. | $0.0124 |
| Punch words | [`caption-emphasis/README.md`](evidence/caption-emphasis/README.md): Spark picked "finished" and "raw" from six lines; both burned in two caption styles. | $0.0004 |
| 3D motion card | [`blender-cards/README.md`](evidence/blender-cards/README.md): Spark designed the card, Muse Image painted the backdrop, Blender 5.2.3 rendered 90 frames in 24 s, and the compiler laid it over the first 3 s with no timing change. | $0.0104 |
| Muse Code | [`muse-code/README.md`](evidence/muse-code/README.md): Meta's coding agent ran a full edit over Briefcut's MCP server: status, import, story, wait, timeline, render, receipts, verify. It reported a 14.1 s 1080x1920 short, 5 of 5 checks, and a verified delivery. | $0.0345 of Briefcut's Meta calls; Muse Code's own 9 calls at most $0.38 |

Some of these proofs used a local whisper.cpp transcript as their input (hero word, the private-mode inputs, robust-features), because they were built while the Meta-only change was landing. Each README says so. [`robust-features/README.md`](evidence/robust-features/README.md) runs the hero word and verified find on public clips of people: the word moved and changed color for contrast on a light background, a silent track was refused before any call, a re-trimmed cut was caught, and searches in Spanish and Chinese verified (21 calls, $0.1094).

## What the scenario matrix found

The matrix lists 11 bugs, ranked. Checked against the code on 2026-11-04:

| # | Bug | Status in the code |
| --- | --- | --- |
| 1 | iPhone HLG video came out with clipped highlights | Fixed: the tone map now uses 203-nit reference white with `desat=0` (`media.py`), shared by single and multi-clip projects |
| 2 | Black borders inside the frame are never detected | Fixed: bars are measured once at ingest and cropped before reframing (`media.detect_borders`) |
| 3 | Multi-clip moments can cross a clip boundary and flash the next clip | Fixed: moments snap to clip edges, and the check "each moment stays inside one clip" guards it (`lint.snap_to_clips`) |
| 4 | The automatic fix can drop the ending the brief asked for | Fixed: it trims the lowest-ranked moment, keeps the named ending, and "the cut ends where the brief asked" checks it (`lint.py`) |
| 5 | The spending cap was not a hard cap for video calls | Fixed: every token count now adds a video allowance above the highest rate measured live (`meta.py`) |
| 6 | The duration check passes cuts far off target (half the target to 15% over) | Fixed: "length fits the target (≤ T s)" treats the target as a ceiling, with a separate note when a cut is well under it |
| 7 | Receipts can say things that did not happen, on silent tracks and single photos | Fixed: silent tracks are not sent to Meta and the receipt says so; a check with nothing to measure is left out; a clamped photo length is a warning |
| 8 | Repeated phrases ("the bowl the bowl") were burned into captions | Fixed: `_collapse_phrases` in `captions.py` |
| 9 | Audio ran 0.07 s past video in one multi-clip render | Fixed: the cut's frame count sets both streams at export, and sound is trimmed to the picture after loudnorm |
| 10 | MCP hid unexpected errors behind "Error executing tool" | Fixed: tools now return a specific, scrubbed message (`mcp_server.py`) |
| 11 | Verified find missed small subjects | Fixed during the run (`WINDOW_HEIGHT = 540` in `verify_find.py`) |

The fixed ones are in the CHANGELOG under 2026-11-04. The matrix also found six problems on the judge path through the README; the quickstart was reordered and corrected from them, and `scripts/studio.sh` no longer lets an empty line in `.env.local` replace a key exported in the shell.


## Earlier live Meta evidence (2026-10-31)

These runs used public procedural examples through the same HTTP routes Studio calls. They prove server behavior, not browser rendering or user experience. All Muse Spark calls used contributor mode. No user footage was uploaded.

They were recorded before Briefcut became Meta-only. In each, the transcript attached to the planning prompt came from a local Whisper model, which has since been removed. Where a row below says "captioned", it names which transcriber made the captions.

| Flow | Evidence and result | Reserved spend |
| --- | --- | ---: |
| Director note | `20261031T153524Z.json`: a typed note produced a v2 edit; downloaded render probe was 3.733 s, 1920×1080, audio and video, BT.709. | $0.0067465 |
| Auto-refine | `20261031T153614Z.json`: on the 3 s `talk.mp4`, Spark's review failed version 1 on hook and pacing (9/11). Auto mode applied two Spark patches, a "Pause in Blue" title and a slow push zoom. Version 2 passed 11/11 and the loop stopped at "All checks passed." Downloaded probe 3.0 s, 1920×1080, audio and video. | $0.0102409 |
| Voice Transcribe short probe | `20261031T153812Z.json`: the live call returned no speech cues for this 3 s excerpt. The plan in this record was local. | $0.00015 |
| Captions and Spanish translation | `20261031-live-translation.json`: Voice Transcribe made 10 English cues, Muse Spark translated all 10, and the Spanish subtitles were burned and visually checked; output probe 40.03 s, 1920×1080, audio and video, BT.709. The plan in this record was local. | $0.0053761 |
| Cutout and generated background | `20261031-live-cutout.json`: Muse Image generated the background, SAM 3.1 masked all 360 frames, mask coverage 100%, 1.5% area flicker; 7/7 checks passed. | $0.0857233 |
| Two-clip project | `20261031-live-multiclip.json`: two clips became a 15 s Spark-planned edit; downloaded probe 1920×1080 with audio and video; 5/5 checks passed. | $0.0033237 |

Successful-run reservations total $0.1115605. An aborted cutout harness attempt made one Spark story call ($0.0006854 reported usage; $0.0033233 reserved); it is not counted in that total.

## What is verified, and what is not

The 2026-10-31 numbers below are from `docs/evidence/`, written by `scripts/demo_run.py` from real HTTP runs on one machine (Apple M5 Max, 128 GiB, macOS 27.0.1, FFmpeg 9.0.2, Python 3.13.16). Timings are wall-clock and include 0.5 s HTTP polling.

| Claim | Evidence | Not established |
| --- | --- | --- |
| A typed director's note became a verified version, with the SAM receipt | `docs/evidence/20261031T083945Z.json` and `latest.md` (2026-10-31T08:39Z, float effect): Spark planned in 63.8 s; SAM 3.1 tracked "the wooden bowl"; Voice Transcribe captioned the cut; Muse Image made the cover. The first verified render (21.6 s) passed 15 of 15 checks, including "auto: subject stays in frame (SAM 3.1)" (452 boxes, 100% coverage, longest gap 0.2 s, centre inside the 9:16 crop in 100% of boxes). `--revise note` with "Hold the finished bowl a beat longer before the first cut, then tighten the sanding moment" ran through the worker: Spark returned two trims, the deterministic lint snapped the sanding trim back to its sentence end at 28.36 s, and version 2 (22.0 s) rendered in 130.5 s. It passed 13 of 15: holding the bowl to 5.58 s pushed the cut inside the next sentence, which both the auto check and Spark flagged. Reserved spend $0.2882 of $0.50. | The receipts caught a note that conflicted with the sentence rule; they did not resolve it. The planning transcript in this run came from local Whisper. A second live note run (`20261031T100101Z-note.json`, "warmer, bigger captions, slow down the oil moment") produced a version 2 that passed 14/14; it captioned with local Whisper, and its version 1 had no Director's notes because a Meta review timed out. Muse Glimmer handled both notes locally on 2026-11-04 (`open-weights/README.md`). |
| The exported file is verified | `report.json` and the job's `result.proof` carry the probe of the delivered file (duration, 1080×1920, h264 30 fps, audio, bt709/tv); the server refuses completion otherwise. `scripts/demo_run.py` also probes the downloaded copy and the two agree (`latest.md`). In the scenario matrix, the probe matched an independent ffprobe on every render but one. | Editorial quality. A passing probe says the file plays with the right shape and color, not that the story is good. In one multi-clip render the audio ran 0.07 s past the video the probe reported (scenario matrix bug 9). |
| Auto-refine fixed failing checks live | `docs/evidence/20261031T141719Z-auto-refine.json` (2026-10-31T14:17Z, `workshop.mp4`, 20 s vertical brief asking for warm golden color and extra large captions): Spark's review failed version 1 on 4 of 16 checks (length past the 20 s ceiling, no warm color, small captions, hard-to-read captions). Auto mode applied 7 Spark patches (Warm look, color temperature 0.35, 2× captions, bold caption style, three moments at 1.2×). Version 2 (19.8 s, 1080×1920, audio and video) passed 16 of 16 and the loop stopped at "All checks passed." Reserved spend $0.0103805. A second run on the 3 s `talk.mp4` (`20261031T153614Z.json`) went from 9/11 to 11/11 after two patches. Frames: `auto-refine-v1-v2-frames.jpg`. | Both passes are graded by the same Spark model that proposed the patches, so 16/16 says Spark is satisfied, not that the cut is good. One round each. No SAM tracking in either run. The `workshop.mp4` run captioned with local Whisper. In the scenario matrix, an automatic fix dropped the photo a brief asked to end on and still passed (bug 4). |
| The full Meta loop ran live, including review and self-correction | `docs/evidence/20261031T070719Z.json` (2026-10-31T07:07Z): Muse Spark (`muse-spark-1.3-contributor`) planned 6 moments on sentence boundaries in 67.6 s (11,682 in / 2,567 out tokens, $0.0017); SAM 3.1 tracked "the wooden bowl"; Voice Transcribe captioned the cut; Muse Image produced the cover; the first verified render (25.03 s) took 290.5 s and passed 13 of 14 checks (Spark failed "pacing" at 4.6 s and proposed two patches). `--revise director` applied one patch (the overlapping one was skipped and reported) and rendered version 2 (21.6 s) in 117.6 s, passing 12 of 13 checks; the remaining failure is Spark noting the cut is now short of 25 s. Reserved spend $0.3131 of a $0.50 cap. Scenario matrix run A5 repeated plan, Voice Transcribe, review, and `revise director` live over MCP on 2026-11-04: 11 of 13, then 13 of 13. | Quality on real footage: the clip is stylised and computer-generated. Contributor mode was used for Spark, which permits Meta to train on the submitted clip. |
| Muse Wonder hears the cut | `wonder-review/README.md`: on the same cut with no captions on screen and no transcript sent, Wonder reported "none" for other sounds, which is true (digital silence between sentences), and called the voice synthetic, which it is. | One procedural clip with a synthetic voice and no music, one run per model. The newer Wonder prompt that fixes its "null" strings was not re-run live. |
| SAM checks what Spark finds | `verified-find/README.md`: verified the bowl in both found moments; flagged the gouge, a SAM false negative on a stylised tool, under two names and two resolutions; scenario matrix A3 verified "a man in a dark suit and purple tie" in 45 of 45 frames. | SAM misses things it has no good name for. One 3 s window checks the middle of each moment, not all of it. |
| Earlier live adapter checks | 2026-10-31T00 to 01Z records: Spark planning (`20261031T004918Z.json`), Voice Transcribe captions and Muse Image cover (`20261031T005146Z.json`), SAM 3.1 tracking of a red square across 300 frames with the reframe keeping it centered (`20261031T005236Z.json`, frames in `sam-tracking-frames.png`), and the center-crop recovery when SAM finds no subject. | These predate sentence snapping, caption polish, lint, and revise. |
| Subject stays in frame | Geometry remapping and tracking tests; `track_reframe` compiles to a bounded, smoothed crop; live SAM runs above; the receipt "auto: subject stays in frame (SAM 3.1)" (`subject_in_frame_check` in `src/vedit/lint.py`) grades box coverage, gaps, and whether the subject centre stays inside the reframed crop. Scenario matrix A1 followed a named subject on a public clip. | The receipt is computed from SAM's boxes, so it inherits their errors. In A1, 79% of the 9:16 frame was the source's own black border, and every check passed (bug 2). |
| Brief-driven edits | `src/vedit/brief_ops.py` with `tests/test_brief_ops.py`; the "auto: requested edits applied" check reports what was and was not applied. The fresh-clone run (scenario matrix C) used the README's example brief: captions, title card, and story order correct, 5 of 5 checks. | The 25-second example came out at 18.9 s and passed, because the duration check accepts half the target to 15% over (bug 6). The title card text comes from the plan title, so a brief that does not name its subject gets a generic title. |
| Spatial effects | `src/vedit/effects.py` presets with validation; rendered locally by `_spatial_effect`; the float effect is in `latest.md`. | These animate a flat plane. They are not depth reconstruction. |
| Script to video | `docs/evidence/20261031-script-to-video.json` (2026-10-31T14:30Z): one Spark script call and 4 Muse Image stills became a 4-scene, 19.67 s, 1080×1920 video. The server proof passed and 5 of 5 automatic checks passed. Reserved spend $0.0433272. Frames: `script-to-video-frames.jpg`. Muse Glimmer wrote script scenes locally on 2026-11-04 (`open-weights/README.md`). | That run was narrated by the macOS voice and timed by local Whisper; both are removed. Today the video is caption-led with no voice, and no live record of that version exists. Stills only, no generated video. |
| Meta calls panel | Built and seen in a browser on a real project: `open-weights/studio-meta-calls-light.png`, `-dark.png`, and `-running.png` show five Meta API calls recorded on 2026-10-31 and two private-mode calls made live on 2026-11-04, one caught mid-call. | No Meta API call has been watched live in the panel yet. The API rows in the screenshots came from that project's existing call log. |
| Evidence labels | Every check in the 2026-11-04 receipts carries `basis`, `by`, and, where a model's data was measured, `inputs`. In scenario matrix A5, 10 checks are `derived` (measured by code) and 16 `inferred` (a model's opinion), across both versions. Studio shows them as Probed, Measured, and Model opinion. | Studio's rendering of the labels has no browser record. The 2026-10-31 runs predate the labels. |
| Saves editing time | Measured machine time only: 63.8 s to a plan and 120.4 s to a verified render (`20261031T083945Z.json`); 67.6 s and 290.5 s (`20261031T070719Z.json`, where stage timestamps show about 14 s of the render was local encoding and the rest was waiting on Meta calls). | A timed comparison against a human editor; none was run. |

### History: local-mode records

Before November 4, Briefcut also had a local mode with no model calls. It has been removed. These records came from it and are kept as history. They do not show Meta's models.

- `20261031T000412Z.json`, `20261031T001626Z.json`: first local runs.
- `20261031T071627Z.json`: the local baseline on `workshop.mp4` (8-moment plan in 1.5 s, verified 25.2 s render in 18.2 s, 5/5 checks, $0).
- `20261031T082624Z.json`: brief-driven edits with the local planner.
- `zsh scripts/real_baseline.sh /tmp/briefcut-real-baseline-final-20261031`: 5/5 local flow checks on four public clips and four of the user's iPhone clips (12.03, 8.87, 10.57, and 11.57 s outputs at 1080×1920). No footage was uploaded. Local Whisper was disabled in that run, so it did not test captions.

## Demo

The three-minute sequence is in `docs/HACKATHON.md`. Record it on the real build with `bash scripts/studio.sh --open`, using `examples/demo/workshop.mp4` or the user's own footage. The demo clip is stylised computer-generated footage, not a recording: five 8 s scenes of an oak bowl on a workbench (finished bowl, raw blank, turning, sanding, oil finish) built from FFmpeg filters with film grain and a vignette, and no baked-in text. Its narration is the macOS `say` voice, added when `scripts/make_demo.py` built the clip; Briefcut does not use `say` at runtime. Say on screen that the clip is generated. Label any prerecorded or sped-up segment.

## Cost model

- Rendering: no API spend. FFmpeg, and Blender for motion cards, run on the user's computer. Private-mode calls cost nothing.
- Meta, measured on the 40 s clip with a 25 s target (`20261031T070719Z.json`, cap $0.50): reserved $0.3131 across two versions, of which SAM tracking was $0.2806 (two calls), Muse Image $0.02 (two covers), Spark planning $0.0034 reserved ($0.0017 used), two Spark reviews $0.0067 reserved ($0.0017 used on the first), Voice Transcribe $0.0024. Tracking is the cost that scales with the cut length. Runs without SAM reserved $0.0067 to $0.0138.
- A Muse Wonder review of an 18.9 s cut cost $0.1010 and reserved $0.3354 (`wonder-review/README.md`). At Spark's standard price the same review would cost about $0.033.
- Every recorded Spark call used contributor mode. Standard mode costs more per token ($1.25 / $4.25 against $0.10 / $0.20 per million).
- The project cap (default $0.25, maximum $5) stops a run before it exceeds the chosen ceiling. The ledger in `meta-usage.json` is a local estimate of reservations and usage, not the provider's bill.

## Track and theme

- **Track:** choose one and state it in the form. Indie developer if submitting as an individual or a team of up to four students or builders; Startup if submitting as Aubric, Inc. with a production story. Judged criteria differ: "creativity, technical ambition, and whether it works" versus "technical execution and whether it holds up in production."
- **Theme:** Build for Your World. Briefcut brings Muse Spark, Muse Wonder, SAM 3.1, Muse Image, Voice Transcribe, and Muse Glimmer into a maker's editing workflow, with the model acting as planner and reviewer and the local compiler keeping the edit reproducible.
- **Reported judging weights** (secondary sources, not confirmed on the official page as of October 30, 2026): Impact 30%, Use of Meta AI tooling 25%, Demo 25%, Technical depth and execution 20%.

## Submission-day checklist

1. From a fresh clone, follow the README quickstart exactly: `uv sync`, `cp .env.example .env.local`, add a key, `bash scripts/studio.sh --open`. Fix anything that does not work as written.
2. `uv run pytest -n auto` is green; note the commit hash. Suite at `0f883c8` on 2026-11-04: 2,662 passed, 1 skipped (the real macOS Keychain), 3 xfailed, in 7:38 on all cores of an Apple M5 Max.
3. Confirm `git ls-files` lists `.env.example` and not `.env.local`, and that no key appears in the repository or its history.
4. Load `.env.local` and run one live Meta loop: `uv run python scripts/demo_run.py --subject "the wooden bowl" --generate-cover --review --revise director --effect push --budget 0.5`. Keep its `docs/evidence/*.json`, `meta-usage.json`, and the delivered `report.json`. `--provider` and `--caption-provider` now accept only `meta`, which is the default.
5. During that run, watch the Meta calls panel show each API call live, and record it. Then update the "Meta calls panel" row above.
6. Run one project from the "Product story, edited" chip with the subject named in the brief, so the title card reads correctly.
7. Record the demo video from a fresh Studio start: one correction, one effect with the tracked subject, the receipts, Director's notes, "Apply director's fixes", the v2 tab, the Meta calls panel, and the opened archive. Show the measured timings and the Meta ledger from that run.
8. Re-read this file. Remove any claim the recorded run did not demonstrate.
9. Confirm the Official Rules regarding pre-existing code, team size, and submission format. As of October 30, 2026, the public page did not state them.
10. Submit before 9:00 PM ET on November 4. Include the repository link, the demo video, this write-up, and the evidence folder.
