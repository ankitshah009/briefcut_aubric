# Briefcut by Aubric.ai: hackathon submission

**One line.** Briefcut turns clips and photos into an editable story and a verified, downloadable campaign. Meta's models propose, track, transcribe, and critique; FFmpeg on the creator's own computer is the only thing that cuts; every version ships with its receipts.

Event: Meta Global AI Developer Hackathon. Submission deadline **November 4, 2026, 9:00 PM ET**. Official event page: <https://dev.meta.ai/events/global-hackathon>. This document is the write-up. Every claim here is meant to stay narrower than the evidence file linked beside it.

## For judges

- **Setup.** The [README quickstart](../README.md#judge-quickstart) runs from a clean clone: uv, FFmpeg, `cp .env.example .env.local`, your own key from <https://dev.meta.ai>, then `bash scripts/studio.sh --open`. The repository contains no key.
- **Which Meta models, and where.** The table in the [README](../README.md#which-meta-models-briefcut-uses-and-where-each-runs) and [below](#how-metas-models-are-used). All four run on the Meta Model API, called from the judge's computer.
- **No key?** The [recorded runs](../README.md#no-key-see-recorded-runs) in `docs/evidence/` hold each run's request, timings, plan, probe of the delivered file, receipts, and Meta ledger. [latest.md](evidence/latest.md) is the best one.
- **What is sent to Meta.** [PRIVACY.md](PRIVACY.md).

## The problem

Creators and small makers have footage and no time. A phone clip of a product being made sits unused because turning it into a short vertical story means scrubbing, choosing moments, cutting on sentences, reframing, captioning, exporting, and checking the file. "AI editors" that do this today are black boxes: you cannot see why a cut was chosen, you cannot re-run the result, you cannot tell the model it was wrong without starting over, and you cannot prove the exported file is what you approved.

## What Briefcut does

1. **Bring in your footage**: up to 30 videos and photos, up to 4 GB each and 2 hours in all. A two-clip project ran live with Muse Spark (`20261031-live-multiclip.json`); that run does not establish quality across arbitrary footage. The original never leaves the machine. Meta receives a reduced copy, an audio track, or text, after the creator confirms.
2. **Describe the outcome** in a sentence: "Open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end." Editing phrases in the brief become engine ops (`src/vedit/brief_ops.py`): pause trimming, a timed title card, fade in and out, denoise, keep original loudness, `hook:` and `cta:` overlays. Each applied or ignored instruction is listed as a decision on the plan.
3. **Get an editable story plan.** Meta Voice Transcribe makes the transcript. Muse Spark watches the footage and proposes the moments, with the transcript and its sentence boundaries offered as preferred cut points. Every cut is then snapped to a sentence boundary by deterministic code (`snap_to_sentences` in `src/vedit/story.py`). Each moment carries a reason and a source time. <!-- VERIFY: planning transcript source is Voice Transcribe after the Meta-only change. -->
4. **Correct it**: move, trim, reorder, add, or delete moments against the source video; edit title and post copy; add one curated spatial effect.
5. **Approve the render.** The approved plan compiles to a YAML brief → `project.json` → `render.sh` → FFmpeg. The job is durable: closing the browser does not stop it.
6. **Verify and read the receipts.** The server probes the delivered file (duration, dimensions, streams, bt709/tv color) and refuses to mark the job complete otherwise. Then `src/vedit/lint.py` grades the cut (cuts on sentence boundaries, duration vs target, moments inside the source and in order, caption coverage, requested edits applied). With review on, Muse Spark adds its own checks. Each check is labeled Probed, Measured, or Model opinion, and each failing check comes with a structured patch.
7. **Revise without starting over**: "Apply director's fixes", "Apply automatic fixes", or a typed director's note render version N+1. Version N stays downloadable with its own report.
8. **Download**: video, cover, post copy, captions, `story.json`, `brief.yaml`, `report.json`.

## What makes it different

The model proposes and critiques; it never executes. Muse Spark returns data (ranges, checks, patches), SAM returns geometry, Voice Transcribe returns cues. The compiler is the only thing that produces executable steps, and a deterministic lint runs after every model patch. A cut left inside a sentence moves to the nearest sentence boundary before anything renders, and anything the lint cannot fix appears as a failed check. Every version ships with receipts: the probe of the file, the list of checks that passed and failed with a jump-to-time, and the exact patches that produced it from the previous version.

## Where it sits next to CapCut

CapCut is the consumer standard for short-form editing. Briefcut's bet is the part its pages do not describe: the model reviews its own render and proposes structured fixes, each version carries pass or fail receipts, and the edit can be re-run as a program. Feature-by-feature status, with sources, is in [CAPCUT_PARITY.md](CAPCUT_PARITY.md).

| | CapCut, from its own pages | Briefcut |
| --- | --- | --- |
| How the cut is chosen | AutoCut proposes an edit from speech analysis, which you refine | Every moment carries a reason and a source timestamp; cuts land on transcript sentences; you edit the plan before anything renders |
| Fixing a bad result | Not described beyond editing by hand | Muse Spark reviews the render and returns structured patches; version N+1 renders and version N stays verified |
| Proof the file is right | Not described | A probe of the delivered file plus pass or fail receipts with jump-to-time, per version |
| Re-running an edit | Not described | `render.sh` re-runs the edit with FFmpeg alone; `brief.yaml` recompiles it |
| Where footage goes | The pages checked link to a privacy policy and do not say | The original stays on the machine. Meta receives a reduced copy, audio, or text after the creator confirms, and Studio lists each Meta call |
| Agent access | A "CapCut × Codex" page describes natural-language instructions producing an editable rough cut; no public API documentation was found | HTTP API and an MCP server that any MCP agent can use: plan, render, revise, receipts |

## How Meta's models are used

Every model Briefcut calls is a Meta model, and every call goes from the user's computer to the Meta Model API (`https://api.meta.ai/v1`). All calls live in one adapter, `src/vedit/meta.py`, behind `MODEL_API_KEY` and `VEDIT_META_ENABLED=1` (or the same two settings in Studio). Models return **data**, never commands; every result is schema-validated before the compiler sees it. Each project carries a spending cap; the adapter reserves the worst-case cost before each call and keeps the reservation after an uncertain failure. Prices below are as encoded in `meta.py` from <https://dev.meta.ai/docs/> on October 29, 2026.

FFmpeg renders on the user's computer and is not a model. Sentence snapping, lint checks, the patcher, and the compiler are deterministic code.

| Capability | Model | Where in the code | What it does in Briefcut |
| --- | --- | --- | --- |
| Transcript and captions | `muse-voice-transcribe-1.0` (`POST /v1/asr/transcribe`) | `meta.transcribe_audio` | The only transcriber. Transcribes mono 16-bit WAV (at most 10 minutes and 32 MB per request) into turn-level cues for the planning transcript and the captions. `polish_cues` in `src/vedit/captions.py` collapses doubled words, merges fragments into sentences, capitalises, and wraps to two balanced lines of at most 42 characters. Captions burn in locally. $0.18 per hour of audio. <!-- VERIFY: planning call site and caption path in campaign.py after Whisper removal. --> |
| Story planning | `muse-spark-1.3`, or `muse-spark-1.3-contributor` with `VEDIT_META_MODE=contributor` | `meta.plan_story`, called from `CampaignManager._plan` in `src/vedit/campaign.py` | Watches a 960×540 analysis proxy of the full project, with the transcript and its sentence boundaries attached as source-clock data, and returns 1 to 8 non-overlapping ranges with visible reasons, title, summary, caption, hashtags, and a cover prompt. The token count is checked with `POST /v1/responses/input_tokens` before the call. $1.25 / $4.25 per million tokens (contributor $0.10 / $0.20). |
| Director's notes and checks | same Spark model | `meta.review_cut`, called after export verification when `review: true` | Reviews a 540p copy of the finished cut and returns a verdict, hook score, strengths, timed fixes, a caption suggestion, structured `checks`, and structured `patches` (`trim_start`, `trim_end`, `remove`, `move_before`, `split`, `set_speed`, `set_edit`). A failed review becomes a warning; the verified file is unchanged. |
| Director's note to patches | same Spark model, text only | `meta.patch_plan`, called from `CampaignManager.revise` with `mode: "note"` | Turns a typed note into the same patch schema. Nothing is uploaded; the outline and sentence list go as text. |
| Find a moment | same Spark model | `meta.find_moments` | Finds where something appears in the footage, on the source clock. Searching what was said runs over the transcript on the user's computer. |
| Long video to shorts | same Spark model | `meta.plan_shorts` | Plans 2 to 5 distinct shorts from one source as sibling projects. |
| Script to video | same Spark model, then `muse-image-1.0` | `meta.script_prompts`, `meta.generate_image` | Spark turns the scenes of a written script into picture prompts; Muse Image makes one still per scene. |
| Caption translation | same Spark model, text only | `meta.translate_cues` | Translates caption text cue by cue; the cue timing is kept. |
| Subject tracking | `sam-3.1` (video segmentation) | `meta.track_subject`; geometry mapped by `remap_proxy_points`; applied by `_track_reframe` in `src/vedit/compile.py` | Follows the named subject across the approved cut only, returns per-frame boxes, and drives a smooth 9:16 reframe of the original footage. $0.20 per 1,000 video frames, 50-frame minimum, at most 15,000 frames. When nothing matches, the job offers a center-crop recovery instead of failing silently. |
| Cutout | `sam-3.1` | `meta.subject_masks`, decoded with the public `meta-sam-parser` decoder; applied by `_cutout` in `src/vedit/compile.py` | Lossless per-frame masks of the named subject. The cutout keeps the subject over a blurred, darkened, black-and-white, solid-color, or new image background. |
| Cover image | `muse-image-1.0` (image edit) | `meta.generate_cover` | Edits a real frame from the delivered video into a PNG cover; opt-in per project. $0.01 per image. |
| Generated background | `muse-image-1.0` | `meta.generate_background` | Makes a background from a description, for the cutout. Needs `consent: true`. $0.01 per image. |

Nothing is sent to Meta by opening Studio. A Meta request happens only after the creator starts a project and ticks the consent box. Studio's Meta calls panel lists each call with its model id, purpose, duration, cost, and request id. <!-- VERIFY: panel name and columns. -->

### Private mode

<!-- VERIFY: placeholder. A private mode running an open-weight Meta model (Muse Glimmer or Muse Spark Open) through llama.cpp on the user's computer, for text-only work (director's notes, translation), is being attempted. Add it to the table above only after a recorded run. -->

Not available in this build.

## Architecture

```text
browser (src/vedit/studio/static)        talks only to 127.0.0.1
   │  upload once · brief · plan · edit · approve · receipts · revise · download
   ▼
HTTP server (src/vedit/studio/server.py)
   ▼
CampaignManager (src/vedit/campaign.py)  SQLite job record · one worker · cancel · retry · versions
   ├── Meta adapter (src/vedit/meta.py)          Voice Transcribe · Spark plan, review, patch, find, shorts, script, translate · SAM 3.1 · Muse Image
   │        spending cap · schema validation · upload cleanup · ledger
   ├── Story (src/vedit/story.py)                scene cuts, speech ranges, sentences; snap_to_sentences
   ├── Brief ops (src/vedit/brief_ops.py)        "trim the pauses", "title card", "fade out" → ops + decisions
   ├── Lint (src/vedit/lint.py)                  deterministic checks + patches; apply_patches is pure
   ▼
approved plan → YAML brief → compiler (src/vedit/compile.py) → project.json + render.sh
   ▼
FFmpeg (ffmpeg-full, VideoToolbox on Apple Silicon) → probe → report.json → checks → campaign.zip
   ▼
POST /api/campaigns/{id}/revise  (director | auto | note) → patches → relint → version N+1
```

The boundary that matters: the model proposes moments, geometry, cues, and patches; the compiler is the only thing that produces executable steps. A judge can open `brief.yaml` from any delivery, and `render.sh` from its project folder, and re-run the edit.

### Agents can drive it

`vedit mcp` exposes the same project flow over MCP (`src/vedit/mcp_server.py`), so any MCP agent can use it: import clips, create a story, wait, render, revise with a mode or a note, find moments, make shorts, turn a script into a video, read the receipts, and resolve artifact paths. The repository ships a project `.mcp.json` and a Claude Code plugin. `vedit tools` remains the older stdio JSON loop, and the HTTP API serves Studio. All three go through the one `CampaignManager`, so an agent gets the same verification and version history a person sees. See "MCP for coding agents" in `docs/CAMPAIGN.md`.

## Live Meta evidence (2026-10-31)

These runs used public procedural examples through the same HTTP routes Studio calls. They prove server behavior, not browser rendering or user experience. All Muse Spark calls used contributor mode, which permits Meta to train on the submitted media. No user footage was uploaded.

They were recorded before Briefcut became Meta-only. In each, the transcript attached to the planning prompt came from a local Whisper model, which has since been removed. Where a row below says "captioned", it names which transcriber made the captions.

| Flow | Evidence and result | Reserved spend |
| --- | --- | ---: |
| Director note | `20261031T153524Z.json`: a typed note produced a v2 edit; downloaded render probe was 3.733 s, 1920×1080, audio and video, BT.709. | $0.0067465 |
| Auto-refine | `20261031T153614Z.json`: on the 3 s `talk.mp4`, Spark's review failed version 1 on hook and pacing (9/11). Auto mode applied two Spark patches, a "Pause in Blue" title and a slow push zoom. Version 2 passed 11/11 and the loop stopped at "All checks passed." Downloaded probe 3.0 s, 1920×1080, audio and video. | $0.0102409 |
| Voice Transcribe short probe | `20261031T153812Z.json`: the live call returned no speech cues for this 3 s excerpt. The plan in this record was local. | $0.00015 |
| Captions and Spanish translation | `20261031-live-translation.json`: Voice Transcribe made 10 English cues, Muse Spark translated all 10, and the Spanish subtitles were burned and visually checked; output probe 40.03 s, 1920×1080, audio and video, BT.709. The plan in this record was local. | $0.0053761 |
| Cutout and generated background | `20261031-live-cutout.json`: Muse Image generated the background, SAM 3.1 masked all 360 frames, mask coverage 100%, 1.5% area flicker; 7/7 checks passed. | $0.0857233 |
| Two-clip project | `20261031-live-multiclip.json`: two clips became a 15 s Spark-planned edit; downloaded probe 1920×1080 with audio and video; 5/5 checks passed. | $0.0033237 |

Successful-run reservations total $0.1115605. An aborted cutout harness attempt made one Spark story call ($0.0006854 reported usage; $0.0033233 reserved); it is not counted in that total. Reservations are local estimates, not a provider invoice. The successful caption flow establishes the Voice Transcribe route; the 3 s probe is a separate no-speech result.

## What is verified, and what is not

All numbers below are from `docs/evidence/`, written by `scripts/demo_run.py` from real HTTP runs on one machine (Apple M5 Max, 128 GiB, macOS 27.0.1, FFmpeg 9.0.2, Python 3.13.16). Timings are wall-clock and include 0.5 s HTTP polling.

| Claim | Evidence | Not established |
| --- | --- | --- |
| A typed director's note became a verified version, with the SAM receipt | `docs/evidence/20261031T083945Z.json` and `latest.md` (2026-10-31T08:39Z, float effect): Spark planned in 63.8 s; SAM 3.1 tracked "the wooden bowl"; Voice Transcribe captioned the cut; Muse Image made the cover. The first verified render (21.6 s) passed 15 of 15 checks, including "auto: subject stays in frame (SAM 3.1)" (452 boxes, 100% coverage, longest gap 0.2 s, centre inside the 9:16 crop in 100% of boxes). `--revise note` with "Hold the finished bowl a beat longer before the first cut, then tighten the sanding moment" ran through the worker: Spark returned two trims, the deterministic lint snapped the sanding trim back to its sentence end at 28.36 s, and version 2 (22.0 s) rendered in 130.5 s. It passed 13 of 15: holding the bowl to 5.58 s pushed the cut inside the next sentence, which both the auto check and Spark flagged. Reserved spend $0.2882 of $0.50. | The receipts caught a note that conflicted with the sentence rule; they did not resolve it. The planning transcript in this run came from local Whisper. A second live note run (`20261031T100101Z-note.json`, "warmer, bigger captions, slow down the oil moment") produced a version 2 that passed 14/14; it captioned with local Whisper, and its version 1 had no Director's notes because a Meta review timed out. |
| The exported file is verified | `report.json` and the job's `result.proof` carry the probe of the delivered file (duration, 1080×1920, h264 30 fps, audio, bt709/tv); the server refuses completion otherwise. `scripts/demo_run.py` also probes the downloaded copy and the two agree (`latest.md`). | Editorial quality. A passing probe says the file plays with the right shape and color, not that the story is good. |
| Auto-refine fixed failing checks live | `docs/evidence/20261031T141719Z-auto-refine.json` (2026-10-31T14:17Z, `workshop.mp4`, 20 s vertical brief asking for warm golden color and extra large captions): Spark's review failed version 1 on 4 of 16 checks (length past the 20 s ceiling, no warm color, small captions, hard-to-read captions). Auto mode applied 7 Spark patches (Warm look, color temperature 0.35, 2× captions, bold caption style, three moments at 1.2×). Version 2 (19.8 s, 1080×1920, audio and video) passed 16 of 16 and the loop stopped at "All checks passed." Reserved spend $0.0103805. A second run on the 3 s `talk.mp4` (`20261031T153614Z.json`) went from 9/11 to 11/11 after two patches. Frames: `auto-refine-v1-v2-frames.jpg`. | Both passes are graded by the same Spark model that proposed the patches, so 16/16 says Spark is satisfied, not that the cut is good. One round each. No SAM tracking in either run. The `workshop.mp4` run captioned with local Whisper. |
| The full Meta loop ran live, including review and self-correction | `docs/evidence/20261031T070719Z.json` (2026-10-31T07:07Z): Muse Spark (`muse-spark-1.3-contributor`) planned 6 moments on sentence boundaries in 67.6 s (11,682 in / 2,567 out tokens, $0.0017); SAM 3.1 tracked "the wooden bowl"; Voice Transcribe captioned the cut; Muse Image produced the cover; the first verified render (25.03 s) took 290.5 s and passed 13 of 14 checks (Spark failed "pacing" at 4.6 s and proposed two patches). `--revise director` applied one patch (the overlapping one was skipped and reported) and rendered version 2 (21.6 s) in 117.6 s, passing 12 of 13 checks; the remaining failure is Spark noting the cut is now short of 25 s. Reserved spend $0.3131 of a $0.50 cap. | Quality on real footage: the clip is stylised and computer-generated. Contributor mode was used for Spark, which permits Meta to train on the submitted clip. |
| Earlier live adapter checks | 2026-10-31T00 to 01Z records: Spark planning (`20261031T004918Z.json`), Voice Transcribe captions and Muse Image cover (`20261031T005146Z.json`), SAM 3.1 tracking of a red square across 300 frames with the reframe keeping it centered (`20261031T005236Z.json`, frames in `sam-tracking-frames.png`), and the center-crop recovery when SAM finds no subject. | These predate sentence snapping, caption polish, lint, and revise. |
| Subject stays in frame | Geometry remapping and tracking tests; `track_reframe` compiles to a bounded, smoothed crop; live SAM runs above; the receipt "auto: subject stays in frame (SAM 3.1)" (`subject_in_frame_check` in `src/vedit/lint.py`) grades box coverage, gaps, and whether the subject centre stays inside the reframed crop. | The receipt is computed from SAM's boxes, so it inherits their errors. Tracking quality on real-world footage with occlusion or camera motion is not established. |
| Brief-driven edits | `src/vedit/brief_ops.py` with `tests/test_brief_ops.py`; the "auto: requested edits applied" check reports what was and was not applied. The only live record (`20261031T082624Z.json`) predates Meta-only and used the old local planner: the "Product story, edited" brief produced "Removed pauses longer than 0.6 s inside the selected moments; Title card for 2.5 s; Fade out 0.5 s", 5/5 checks, 23.0 s output. | No record with a Spark plan yet. The title card text comes from the plan title, so a brief that does not name its subject gets a generic title ("The product"). Name the subject in the brief. |
| Spatial effects | `src/vedit/effects.py` presets with validation; rendered locally by `_spatial_effect`; the float effect is in `latest.md`. | These animate a flat plane. They are not depth reconstruction. |
| Script to video | `docs/evidence/20261031-script-to-video.json` (2026-10-31T14:30Z): one Spark script call and 4 Muse Image stills became a 4-scene, 19.67 s, 1080×1920 video with audio. The server proof passed and 5 of 5 automatic checks passed (98% of 17.5 s of speech captioned). Reserved spend $0.0433272. Frames: `script-to-video-frames.jpg`. | That run was narrated by the macOS voice Samantha and timed by local Whisper; both have since been removed. <!-- VERIFY: how script to video narrates and times scenes in Meta-only mode, and whether a new record exists. --> Stills only, no generated video. The record holds the server proof, not a separate probe of a downloaded copy. Every picture is generated, and the plan says so. |
| Meta calls panel | Built, browser-checked, awaiting a live Meta run. Studio lists each Meta call with model id, purpose, duration, cost, and request id. <!-- VERIFY: file names, test names, and the browser check record. --> | No live Meta run has been shown in the panel yet. |
| Evidence labels | Built, browser-checked, awaiting a live Meta run. Each check in the receipts says how it is known: Probed, Measured (computed by code), or Model opinion (Spark's review). The summary reads like "12 measured, 4 model opinions". <!-- VERIFY: confirm "evidence labels" means these receipt tags, or name what else it covers. --> | The recorded runs above predate the labels. |
| Saves editing time | Measured machine time only: 63.8 s to a plan and 120.4 s to a verified render (`20261031T083945Z.json`); 67.6 s and 290.5 s (`20261031T070719Z.json`, where stage timestamps show about 14 s of the render was local encoding and the rest was waiting on Meta calls). | A timed comparison against a human editor; none was run. |

### History: local-mode records

Before November 4, Briefcut also had a local mode with no model calls. It has been removed. These records came from it and are kept as history. They do not show Meta's models.

- `20261031T000412Z.json`, `20261031T001626Z.json`: first local runs.
- `20261031T071627Z.json`: the local baseline on `workshop.mp4` (8-moment plan in 1.5 s, verified 25.2 s render in 18.2 s, 5/5 checks, $0).
- `20261031T082624Z.json`: brief-driven edits with the local planner.
- `zsh scripts/real_baseline.sh /tmp/briefcut-real-baseline-final-20261031`: 5/5 local flow checks on four public clips and four of the user's iPhone clips (12.03, 8.87, 10.57, and 11.57 s outputs at 1080×1920). No footage was uploaded. Local Whisper was disabled in that run, so it did not test captions.

## Demo

The three-minute sequence is in `docs/HACKATHON.md`. Record it on the real build with `bash scripts/studio.sh --open`, using `examples/demo/workshop.mp4` or the user's own footage. The demo clip is stylised computer-generated footage with a synthetic voice, not a recording: five 8 s scenes of an oak bowl on a workbench (finished bowl, raw blank, turning, sanding, oil finish) built from FFmpeg filters with film grain and a vignette, and no baked-in text (`uv run python scripts/make_demo.py`). Say so on screen. Label any prerecorded or sped-up segment. <!-- VERIFY: make_demo.py narration source after `say` removal. -->

## Cost model

- Rendering: no API spend. FFmpeg runs on the user's computer.
- Meta, measured on the 40 s clip with a 25 s target (`20261031T070719Z.json`, cap $0.50): reserved $0.3131 across two versions, of which SAM tracking was $0.2806 (two calls), Muse Image $0.02 (two covers), Spark planning $0.0034 reserved ($0.0017 used), two Spark reviews $0.0067 reserved ($0.0017 used on the first), Voice Transcribe $0.0024. Tracking is the cost that scales with the cut length. Runs without SAM reserved $0.0067 to $0.0138 (`20261031T153524Z.json`, `20261031T153614Z.json`, `20261031T141719Z-auto-refine.json`, `20261031T100101Z-note.json`).
- Every recorded Spark call used contributor mode. Standard mode costs more per token ($1.25 / $4.25 against $0.10 / $0.20 per million).
- The project cap (default $0.25, maximum $5) stops a run before it exceeds the chosen ceiling. The ledger in `meta-usage.json` is a local estimate of reservations and usage, not the provider's bill.

## Track and theme

- **Track:** choose one and state it in the form. Indie developer if submitting as an individual or a team of up to four students or builders; Startup if submitting as Aubric.ai with a production story. Judged criteria differ: "creativity, technical ambition, and whether it works" versus "technical execution and whether it holds up in production."
- **Theme:** Build for Your World. Briefcut brings Muse Spark, SAM 3.1, Muse Image, and Voice Transcribe into a maker's editing workflow, with the model acting as planner and reviewer and the local compiler keeping the edit reproducible.
- **Reported judging weights** (secondary sources, not confirmed on the official page as of October 30, 2026): Impact 30%, Use of Meta AI tooling 25%, Demo 25%, Technical depth and execution 20%.

## Submission-day checklist

1. From a fresh clone, follow the README quickstart exactly: `uv sync`, `uv run vedit doctor`, `cp .env.example .env.local`, add a key, `bash scripts/studio.sh --open`. Fix anything that does not work as written.
2. `uv run pytest -n auto` is green; note the commit hash.
3. Confirm `git ls-files` lists `.env.example` and not `.env.local`, and that no key appears in the repository or its history.
4. Load `.env.local` and run one live Meta loop: `uv run python scripts/demo_run.py --provider meta --subject "the wooden bowl" --caption-provider meta --generate-cover --review --revise director --effect push --budget 0.5`. Keep its `docs/evidence/*.json`, `meta-usage.json`, and the delivered `report.json`. <!-- VERIFY: demo_run.py flags after the Meta-only change (`--provider` and `--caption-provider` may be gone). -->
5. During that run, check that the Meta calls panel shows each call, and record it. Then update the two "awaiting a live Meta run" rows above.
6. Run one project from the "Product story, edited" chip with the subject named in the brief, so the title card reads correctly.
7. Record the demo video from a fresh Studio start: one correction, one effect with the tracked subject, the receipts, Director's notes, "Apply director's fixes", the v2 tab, the Meta calls panel, and the opened archive. Show the measured timings and the Meta ledger from that run.
8. Re-read this file. Remove any claim the recorded run did not demonstrate.
9. Confirm the Official Rules regarding pre-existing code, team size, and submission format. As of October 30, 2026, the public page did not state them.
10. Submit before 9:00 PM ET on November 4. Include the repository link, the demo video, this write-up, and the evidence folder.
