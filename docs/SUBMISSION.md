# Briefcut by Aubric.ai: hackathon submission

**One line.** Briefcut turns one clip into an editable story and a verified, downloadable campaign: Meta's models propose, track, and critique; the local `vedit` compiler is the only thing that cuts; every version ships with its receipts.

Event: Meta Global AI Developer Hackathon. Submission deadline **November 4, 2026, 9:00 PM ET**. Official event page: <https://dev.meta.ai/events/global-hackathon>. This document is the write-up; keep every claim narrower than the evidence linked from it.

## The problem

Creators and small makers have footage and no time. A phone clip of a product being made sits unused because turning it into a short vertical story means scrubbing, choosing moments, cutting on sentences, reframing, captioning, exporting, and checking the file. "AI editors" that do this today are cloud black boxes: you cannot see why a cut was chosen, you cannot re-run the result, you cannot tell the model it was wrong without starting over, and you cannot prove the exported file is what you approved.

## What Briefcut does

1. **Upload one clip** (MP4, MOV, WebM, up to 200 MB, up to 20 minutes). The original never leaves the machine unless the creator chooses Meta director and confirms it.
2. **Describe the outcome** in a sentence: "Open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end." Editing phrases in the brief become engine ops (`src/vedit/brief_ops.py`): pause trimming, a timed title card, fade in/out, denoise, keep original loudness, `hook:` and `cta:` overlays. Each applied or ignored instruction is listed as a decision on the plan.
3. **Get an editable story plan**: source-clock ranges with a reason for each, every cut snapped to a transcript sentence boundary (`snap_to_sentences` in `src/vedit/story.py`). In Local mode this is deterministic analysis (scene cuts, speech ranges, transcript sentences, scene coverage). In Meta mode, Muse Spark watches the footage and proposes the moments; the prompt lists the sentence boundaries as preferred cut points and the same snap runs on its answer.
4. **Correct it**: move, trim, reorder, add, or delete moments against the source video; edit title and post copy; add one curated spatial effect.
5. **Approve the render**. The approved plan compiles to a YAML brief → `project.json` → `render.sh` → FFmpeg. The job is durable: closing the browser does not stop it.
6. **Verify and read the receipts**: the server probes the delivered file (duration, dimensions, streams, bt709/tv color) and refuses to mark the job complete otherwise. Then `src/vedit/lint.py` grades the cut (cuts on sentence boundaries, duration vs target, moments inside the source and in order, caption coverage, requested edits applied) and, in Meta mode with review on, Muse Spark adds its own checks. Each failing check comes with a structured patch.
7. **Revise without starting over**: "Apply director's fixes", "Apply automatic fixes", or a typed director's note render version N+1. Version N stays downloadable with its own report.
8. **Download**: video, cover, post copy, `story.json`, `brief.yaml`, `report.json`.

## What makes it different

The model proposes and critiques; it never executes. Muse Spark returns data (ranges, checks, patches), SAM returns geometry, Voice Transcribe returns cues. The compiler is the only thing that produces executable steps, and a deterministic lint runs after every model patch so a cut that lands inside a sentence is snapped back before anything renders. Every version ships with receipts: the probe of the file, the list of checks that passed and failed with a jump-to-time, and the exact patches that produced it from the previous version.

## Where it sits next to CapCut

CapCut is the consumer standard for short-form editing: templates, effects, auto captions, auto-cut, and script-to-video, free for most features. Briefcut does not try to match that surface. The comparison that matters for a maker or a developer is what happens between the brief and the file:

| | CapCut | Briefcut |
| --- | --- | --- |
| How the cut is chosen | Opaque auto-edit; you accept or redo it | Every moment carries a reason and a source timestamp; cuts land on transcript sentences; you edit the plan before anything renders |
| Fixing a bad result | Re-run or hand-edit | The model watches its own render and returns structured patches; one click renders version N+1 and version N stays verified |
| Proof the file is right | None beyond playback | A probe of the delivered file plus pass/fail receipts with jump-to-time, per version |
| Reproducibility | None | `brief.yaml`, `project.json`, and `render.sh` re-run the edit without Briefcut |
| Where footage goes | ByteDance cloud for AI features | Stays on the machine unless the creator opts into Meta mode and confirms |
| Agent access | None | HTTP API and MCP tools (plan, render, revise, receipts) |
| Effects, templates, music library | Extensive | Three curated spatial effects, pause trimming, title card, fades, denoise |

Briefcut is behind CapCut on breadth and polish and should say so. It is ahead on inspectability, verification, and the self-correcting loop, which is the part CapCut does not expose. That is the product bet for the hackathon, not feature parity.

## How Meta's models are used

All Meta calls live in one adapter, `src/vedit/meta.py`, behind `MODEL_API_KEY` and `VEDIT_META_ENABLED=1`. Models return **data**, never commands; every result is schema-validated before the compiler sees it. Each campaign carries a spending cap; the adapter reserves the worst-case cost before each call and keeps the reservation after an uncertain failure. Prices below are from <https://dev.meta.ai/docs/pricing-rate-limits> as of October 30, 2026.

| Capability | Model | Where | What it does in Briefcut |
| --- | --- | --- | --- |
| Story planning | `muse-spark-1.3` (video understanding, strict JSON schema output); `muse-spark-1.3-contributor` with `VEDIT_META_MODE=contributor` | `meta.plan_story`, called from `CampaignManager._plan` in `src/vedit/campaign.py` | Watches a 960×540 analysis proxy of the full clip, with the local transcript and its sentence boundaries attached as source-clock data, and returns 1–8 non-overlapping ranges with visible reasons, title, summary, caption, hashtags, and a cover prompt. Token count is checked with `POST /v1/responses/input_tokens` before the call. $1.25 / $4.25 per million tokens (contributor $0.10 / $0.20). |
| Director's notes and checks | same Spark model | `meta.review_cut`, called from `CampaignManager._review_cut` after export verification when `review: true` | Reviews a 540p copy of the finished cut and returns a verdict, hook score, strengths, timed fixes, a caption suggestion, structured `checks` (one per requirement in the goal, plus hook, mid-sentence cuts, pacing, legibility) and structured `patches` (`trim_start`, `trim_end`, `remove`, `move_before`) against the approved outline. A failed review becomes a warning; the verified file is unchanged. |
| Director's note to patches | same Spark model, text only | `meta.patch_plan`, called from `CampaignManager.revise` with `mode: "note"` | Turns a typed note into the same patch schema. Nothing is uploaded; the outline and sentence list are passed as text. Ledger operation `patch`. |
| Subject tracking | `sam-3.1` (video segmentation) | `meta.track_subject`, called from `CampaignManager._render`; geometry mapped by `remap_proxy_points`; applied by `_track_reframe` in `src/vedit/compile.py` | Follows the named subject across the approved cut only, returns per-frame boxes, and drives a smooth 9:16 reframe of the original footage. $0.20 per 1,000 video frames, 50-frame minimum, ≤15,000 frames. When nothing matches, the job offers a center-crop recovery instead of failing silently. |
| Cover image | `muse-image-1.0` (image edit) | `meta.generate_cover`, called from `CampaignManager._render` | Edits a real frame from the delivered video into a PNG cover; opt-in per campaign. $0.01 per image. |
| Captions | `muse-voice-transcribe-1.0` (`POST /v1/asr/transcribe`) | `meta.transcribe_audio`, used by the captions path in `src/vedit/campaign.py` when `caption_provider` is `meta` | Transcribes the approved cut's audio (mono 16-bit WAV, ≤10 min, ≤32 MB) into turn-level cues. `polish_cues` in `src/vedit/captions.py` collapses doubled words, merges fragments into sentences, capitalises, and wraps to two balanced lines of at most 42 characters; the same polish runs on local Whisper. Burned in locally. $0.18 per hour of audio. Local Whisper remains the default. |

Local mode makes **no** model calls. Nothing is sent to Meta by opening Studio; a Meta request happens only after the creator picks "Meta director" and confirms the consent checkbox.

## Architecture

```text
browser (src/vedit/studio/static)        local only, 127.0.0.1
   │  upload once · brief · plan · edit · approve · receipts · revise · download
   ▼
HTTP server (src/vedit/studio/server.py)
   ▼
CampaignManager (src/vedit/campaign.py)  SQLite job record · one worker · cancel · retry · versions
   ├── Local planner (src/vedit/story.py)        scenes + speech + sentences; snap_to_sentences
   ├── Brief ops (src/vedit/brief_ops.py)        "trim the pauses", "title card", "fade out" → ops + decisions
   ├── Meta adapter (src/vedit/meta.py)          Spark plan · Spark review/patch · SAM 3.1 · Muse Image · Voice Transcribe
   │        budget ledger · schema validation · upload cleanup
   ├── Lint (src/vedit/lint.py)                  deterministic checks + patches; apply_patches is pure
   ▼
approved plan → YAML brief → compiler (src/vedit/compile.py) → project.json + render.sh
   ▼
FFmpeg (ffmpeg-full, VideoToolbox on Apple Silicon) → probe → report.json → checks → campaign.zip
   ▼
POST /api/campaigns/{id}/revise  (director | auto | note) → patches → relint → version N+1
```

The boundary that matters: the model proposes moments, geometry, and patches; the compiler is the only thing that produces executable steps. A judge can open `brief.yaml` and `render.sh` from any delivery and re-run the edit without Briefcut.

### Agents can drive it

`vedit mcp` exposes the same campaign flow to coding agents over MCP (`src/vedit/mcp_server.py`): import a clip, create a story, wait, render, revise with a mode or a note, read the receipts, and resolve artifact paths on disk. `vedit tools` remains the older stdio JSON loop, and the HTTP API serves Studio. All three go through the one `CampaignManager`, so an agent gets the same verification and the same version history a person sees. See "MCP for coding agents" in `docs/CAMPAIGN.md`.

## What is verified, and what is not

All numbers below are from `docs/evidence/`, written by `scripts/demo_run.py` from real HTTP runs on one machine (Apple M5 Max, 128 GiB, macOS 27.0.1, FFmpeg 9.0.2, Python 3.13.16). Timings are wall-clock and include 0.5 s HTTP polling.

| Claim | Evidence | Not established |
| --- | --- | --- |
| The local flow works end to end | `docs/evidence/20261031T071627Z.json`: the 40.0 s `workshop.mp4` reached an 8-moment editable plan in 1.5 s and a verified 25.2 s 1080×1920 render with local captions and the push-in effect in 18.2 s. All 5 automatic checks passed (every cut on a sentence boundary; 100% of 22.6 s of speech captioned). $0 spend. | Behavior on other hardware. Re-run `uv run python scripts/demo_run.py` on the demo machine the day of submission; `docs/evidence/latest.md` is overwritten by the most recent run of either mode. |
| The exported file is verified | `report.json` and the job's `result.proof` carry the probe of the delivered file (duration, 1080×1920, h264 30 fps, audio, bt709/tv); the server refuses completion otherwise. The script also probes the downloaded copy and the two agree. | Editorial quality. A passing probe says the file plays with the right shape and color, not that the story is good. |
| A typed director's note became a verified version, with the SAM receipt | `docs/evidence/20261031T083945Z.json` and `latest.md` (2026-10-31T08:39Z, float effect, director prompt treating the named length as a ceiling): the first verified render (21.6 s) passed 15 of 15 checks, including "auto: subject stays in frame (SAM 3.1)" (452 boxes, 100% coverage, longest gap 0.2 s, centre inside the 9:16 crop in 100% of boxes). `--revise note` with "Hold the finished bowl a beat longer before the first cut, then tighten the sanding moment" ran through the worker: Spark returned two trims, the deterministic lint snapped the sanding trim back to its sentence end at 28.36 s, and version 2 (22.0 s) rendered in 130.5 s. It passed 13 of 15: holding the bowl to 5.58 s pushed the cut inside the next sentence, which both the auto check and Spark flagged. Reserved spend $0.2882 of $0.50. | The receipts caught a note that conflicted with the sentence rule; they did not resolve it. `auto` mode has a live record only as the lint pass inside the note revision; it is otherwise test-verified. |
| The full Meta loop ran live, including review and self-correction | `docs/evidence/20261031T070719Z.json` and `latest.md` (2026-10-31T07:07Z): Muse Spark (`muse-spark-1.3-contributor`) planned 6 moments on sentence boundaries in 67.6 s (11,682 in / 2,567 out tokens, $0.0017); SAM 3.1 tracked "the wooden bowl"; Meta Voice Transcribe captioned the cut; Muse Image produced the cover; the first verified render (25.03 s) took 290.5 s and passed 13 of 14 checks (Spark failed "pacing" at 4.6 s and proposed two patches). `--revise director` applied one patch (the overlapping one was skipped and reported) and rendered version 2 (21.6 s) in 117.6 s, passing 12 of 13 checks; the remaining failure is Spark noting the cut is now short of 25 s. Reserved spend $0.3131 of a $0.50 cap. | Quality on real footage: the clip is stylised, computer-generated. Contributor mode was used for Spark, which permits Meta to train on the submitted clip. The `note` mode has its own live record above; `auto` is covered by HTTP tests (`tests/test_campaign_http.py`) and the pure patcher tests (`tests/test_lint.py`). |
| Earlier live adapter checks | 2026-10-31T00–01Z records: Spark planning (`20261031T004918Z.json`), Voice Transcribe captions and Muse Image cover (`20261031T005146Z.json`), SAM 3.1 tracking of a red square across 300 frames with the reframe keeping it centered (`20261031T005236Z.json`, frames in `sam-tracking-frames.png`), and the center-crop recovery when SAM finds no subject. | These predate sentence snapping, caption polish, lint, and revise. |
| Subject stays in frame | Geometry remapping and tracking tests; `track_reframe` compiles to a bounded, smoothed crop; live SAM runs above; the receipt "auto: subject stays in frame (SAM 3.1)" (`subject_in_frame_check` in `src/vedit/lint.py`) grades box coverage, gaps, and whether the subject centre stays inside the reframed crop. | The receipt is computed from SAM's boxes, so it inherits their errors. Tracking quality on real-world footage with occlusion or camera motion is not established. |
| Brief-driven edits | `src/vedit/brief_ops.py` with `tests/test_brief_ops.py`; the "auto: requested edits applied" check reports what was and was not applied. Live local record `docs/evidence/20261031T082624Z.json`: the "Product story, edited" brief produced "Removed pauses longer than 0.6 s inside the selected moments; Title card for 2.5 s; Fade out 0.5 s", 5/5 checks, 23.0 s output. | The title card text comes from the plan title, so a brief that does not name its subject gets a generic title ("The product"). Name the subject in the brief. |
| Spatial effects | `src/vedit/effects.py` presets with validation; rendered locally by `_spatial_effect`; the push-in effect is in both runs above. | These animate a flat plane. They are not depth reconstruction. |
| Saves editing time | Measured machine time only: 1.5 s to a plan and 18.2 s to a verified vertical render locally; 67.6 s and 290.5 s with the full Meta loop (stage timestamps show about 14 s of that render was local encoding; the rest was waiting on Meta calls). | A timed comparison against a human editor; none was run. |

## Demo

The three-minute sequence is in `docs/HACKATHON.md`. Record it on the real build with `./scripts/studio.sh --open`, using `examples/demo/workshop.mp4` or your own footage. The demo clip is stylised computer-generated footage, not a real recording: five 8 s scenes of an oak bowl on a workbench (finished bowl, raw blank, turning, sanding, oil finish) built from FFmpeg filters with film grain and a vignette, no baked-in text, and narration from the macOS `say` voice (`uv run python scripts/make_demo.py`). Say so on screen. Label any prerecorded or sped-up segment.

## Cost model

- Local edit: $0. FFmpeg, whisper.cpp, and the planner run on the machine.
- Meta mode, measured on the 40 s clip with a 25 s target (`20261031T070719Z.json`, cap $0.50): reserved $0.3131 across two versions, of which SAM tracking was $0.2806 (two calls), Muse Image $0.02 (two covers), Spark planning $0.0034 reserved ($0.0017 used), two Spark reviews $0.0067 reserved ($0.0017 used on the first), Voice Transcribe $0.0024. Tracking is the cost that scales with the cut length.
- The campaign cap (default $0.25, maximum $5) stops a run before it exceeds the chosen ceiling. The ledger in `meta-usage.json` is a local estimate of reservations and usage, not the provider's bill. Prices are encoded as of October 29, 2026.

## Track and theme

- **Track:** choose one and state it in the form. Indie developer if submitting as an individual or a team of up to four students/builders; Startup if submitting as Aubric.ai with a production story. Judged criteria differ: "creativity, technical ambition, and whether it works" versus "technical execution and whether it holds up in production."
- **Theme:** Build for Your World. Briefcut brings Muse Spark, SAM 3.1, Muse Image, and Voice Transcribe into a maker's editing workflow, with the model acting as planner and reviewer and the local compiler keeping the edit reproducible.
- **Reported judging weights** (secondary sources, not confirmed on the official page as of October 30, 2026): Impact 30%, Use of Meta AI tooling 25%, Demo 25%, Technical depth and execution 20%.

## Submission-day checklist

1. `uv run vedit doctor` passes; `uv run pytest -n auto` is green; note the commit hash.
2. Export `MODEL_API_KEY`, `VEDIT_META_ENABLED=1`; run `uv run python scripts/demo_run.py --provider meta --subject "the wooden bowl" --caption-provider meta --generate-cover --review --revise director --effect push --budget 0.5` and keep its `docs/evidence/*.json`, `meta-usage.json`, and the delivered `report.json`.
3. Run `uv run python scripts/demo_run.py` for the local baseline on the demo machine; commit `docs/evidence/`.
4. Run one campaign from the "Product story, edited" chip with the subject named in the brief, so the title card reads correctly on the demo machine.
5. Record the demo video from a fresh Studio start: one correction, one effect with the tracked subject, the receipts, Director's notes, "Apply director's fixes", the v2 tab, and the opened archive. Show the measured timings and the Meta ledger from that run.
6. Re-read this file. Remove any claim the recorded run did not demonstrate.
7. Confirm the Official Rules (published on the Cerebral Valley event page before the start) regarding pre-existing code, team size, and submission format. As of October 30, 2026, the public page did not state them; the waiver page did not load for us.
8. Submit before 9:00 PM ET on November 4. Include the repository link, the demo video, this write-up, and the evidence folder.
