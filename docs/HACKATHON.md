# Briefcut by Aubric, Inc. demo

The product promise is simple: turn real footage into a story worth watching. Demonstrate the creator inspecting, revising, and exporting that story. Use **Briefcut by Aubric, Inc.** as the product attribution; `vedit` remains the engine and command-line interface. See [Brand](BRAND.md).

The target is the Meta Global AI Developer Hackathon. Its published submission cutoff is **November 4, 2026, at 9:00 PM ET**. Applications closed October 25. Confirm the team's acceptance, track, and the current rules governing pre-existing code before submitting this repository. This document does not establish eligibility. [Official event details](https://dev.meta.ai/events/global-hackathon) · [Submission write-up](SUBMISSION.md)

Final-project judging weights **as reported by secondary coverage** (not shown on the official page as of October 30, 2026): Impact 30%, Use of Meta AI tooling 25%, Demo 25%, Technical depth and execution 20%. The official page states track criteria without weights: Indie developer is "judged on creativity, technical ambition, and whether it works"; Startup is "judged on technical execution and whether it holds up in production." Themes are "Build for Your World" and "Push the Frontier." Official Rules were announced for publication on the Cerebral Valley event page before October 26; the public page did not show them when checked, and the waiver page did not load. Read them before submitting.

Organizer guidance for judging, from the official Discord bot: judges work from the public GitHub repository and the 3-minute video; the repository must run from its setup instructions; judges may use their own keys; the README should say which Meta models are used and where they run; and judges should have a way to see features without a key, such as recorded sample runs with their outputs. The [README](../README.md) covers each point: the model table, the judge quickstart with `.env.example`, and the recorded runs. A fresh clone was run through that quickstart on 2026-11-04 ([scenario matrix, run C](evidence/scenario-matrix/README.md)), and the README was corrected from what it found.

For the reasoning and the design, point judges to [WHY.md](WHY.md) and [ARCHITECTURE.md](../ARCHITECTURE.md). Use the architecture diagrams to explain the boundary between what the models propose and what the compiler executes.

Demo clip: `examples/demo/workshop.mp4` is stylised computer-generated footage, not a recording. `uv run python scripts/make_demo.py` builds five 8 s scenes of an oak bowl on a workbench (finished bowl, raw oak blank, turning with gouge and shavings, sanding, oil finish) from FFmpeg filters with film grain and a vignette, no baked-in text, and narration in the macOS `say` voice with one-second pauses between sentences. `say` runs only when the script builds the clip; Briefcut does not use it at runtime. The clip gives scene, silence, transcript, and subject-tracking analysis real structure without customer footage. Say on screen that it is generated. Prefer your own footage for the recording when you have permission.

## Three-minute demonstration

| Time | Show | Explain |
| --- | --- | --- |
| 0:00–0:15 | The maker's clip, including the slow beginning and the useful action. State that the demo clip is generated footage if you use `workshop.mp4`. | Name the person and the editing task they need to finish: a 25-second vertical story. |
| 0:15–0:35 | Add one or more clips in the project editor. Pick the "Product story, edited" chip, or type a brief with an edit instruction: "Make a 25-second vertical story: open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end." Tick "Send this footage to Meta using my API key" and "Ask Meta to review the finished cut". For a cut where sound matters, choose Muse Wonder as the reviewer and raise the project API limit to $0.50 or more first. | The project editor accepts up to 30 clips and photos; the brief is the source of truth. Editing phrases become engine ops and appear as decisions on the plan. The creator confirms before any footage goes to Meta. |
| 0:35–1:00 | Show the plan: each moment's reason, the scrubber ticks, and that every cut sits on a sentence boundary. Jump to one moment and correct it. | Meta Voice Transcribe made the transcript. Muse Spark proposed the moments with that transcript and its sentence boundaries as data, and deterministic code snapped the cuts to finished sentences. The user can inspect and correct every moment. |
| 1:00–1:20 | Add one effect (Floating frame reads best on a vertical) and name the subject ("the wooden bowl"). | Curated effects render locally over a blurred copy of the footage. SAM 3.1 supplies geometry for the 9:16 reframe, and the receipts grade that track ("subject stays in frame"). If the director finds nothing to fix, type a note instead; it becomes patches through Muse Spark. |
| 1:20–1:50 | Start the render; show its real stages (tracking, transcribing, encoding, reviewing, cover). Open the Meta calls panel with the **Meta calls** button in the top bar (a pulse line with a count) and let it fill in. Play the finished short. | Encoding runs on this computer; the Meta stages are the slow part. Each Meta call shows its purpose, status, model, cost, duration, and Meta's response id. |
| 1:50–2:15 | Open "The receipts": the probe, the automatic checks, the director's checks with jump-to-time. Read the Director's notes and the one failing check. | Each check is labeled Probed, Measured, or Model opinion. A verified file is not a good story. If Muse Wonder reviewed it, point to its audio checks. |
| 2:15–2:40 | Click "Apply director's fixes" or "Apply automatic fixes". Show the v2 tab appear, the "What changed in v2" list (including a skipped patch, if any), and that v1 is still downloadable. | The model proposed patches; the pure patcher applied them; lint re-ran; the compiler rendered. Nothing from the model executed. In the live auto-refine run, version 1 failed 4 of 16 checks and version 2 passed 16 of 16 (`docs/evidence/20261031T141719Z-auto-refine.json`). |
| 2:40–2:55 | Download the campaign archive with the Muse Image cover. Open it. | The output is ready for a creator's publishing workflow. It has not been posted automatically. |
| 2:55–3:00 | Show the Run facts and the Meta calls panel: time to plan, time to render, and spend from this run. | Explain the observed benefit and the limits plainly. |


Most steps above have a record in `docs/evidence/`, though not all in one run. The brief-driven edits record (`20261031T082624Z.json`) predates Meta-only and used the old local planner. The two-clip Meta project ran without review (`20261031-live-multiclip.json`). These records come from the HTTP routes and MCP. The only browser record is the Meta calls panel on a real project, with two private-mode calls made live (`open-weights/studio-meta-calls-*.png`); no Meta API call has been watched live in it yet. Rehearse the full sequence in Studio before recording.

If time allows a cutaway, these have live records:

- the hero word behind the subject (`hero-word/`), punch words in the captions (`caption-emphasis/`), and a 3D motion card rendered by Blender (`blender-cards/`; needs Blender on the demo machine),
- verified find, where SAM 3.1 checks what Muse Spark found (`verified-find/`),
- Muse Wonder hearing the cut where Muse Spark did not (`wonder-review/`),
- private mode, with a director's note answered by Muse Glimmer on the same Mac and tagged "Private" in the Meta calls panel (`open-weights/`),
- Muse Code driving Briefcut over MCP (`muse-code/`),
- the cutout with a generated background (`20261031-live-cutout.json`), Spanish captions (`20261031-live-translation.json`), and script to video from Muse Image stills (`20261031-script-to-video.json`, recorded when it still had a voice; it is caption-led now).

Long-video-to-shorts has no live record, so leave it out of the recording.

Adjust the recording to actual render time. The full live Meta loop took 63.8 s to plan and 120.4 s to render version 1 in `docs/evidence/latest.md` (`20261031T083945Z.json`), and 67.6 s and 290.5 s in `20261031T070719Z.json`, so cut or label the wait. If a stage is sped up or a result was prepared earlier, label it. A prerecorded result can support a live demo but must not impersonate a live model call.

## Prepare evidence

- Use footage you own or have permission to demonstrate. Select a clip where the desired story and subject are visually obvious. Remember that every project sends a reduced copy to Meta, and contributor mode lets Meta train on it.
- From a fresh clone, follow the README quickstart exactly as written. The organizers say the repository must run from its setup instructions.
- Start the checkout with `bash scripts/studio.sh --open`, which loads `.env.local` and runs `vedit doctor`. The launcher creates the environment with `uv` if `.venv` is missing. The packaged app takes its key from Settings.
- Run `uv run python scripts/demo_run.py --subject "the wooden bowl" --generate-cover --review --revise director --budget 0.5` with `.env.local` loaded. `--provider` and `--caption-provider` accept only `meta`, the default. It drives the real HTTP flow (upload, plan, approve, render, probe, checks) and writes `docs/evidence/<timestamp>.json` and `docs/evidence/latest.md` with measured time to plan, time to render, machine details, the probe of the downloaded file, the receipts, and the version table. `--revise auto` and `--revise note --note "..."` exercise the other two revision modes. It overwrites `latest.md` on every run, so restore it to the best honest run afterwards.
- Save the source, approved plan, generated program, output, verification report, `meta-usage.json`, and relevant test results.
- Record time to first useful plan, time to completed video, input duration and resolution, and the hardware used. `scripts/demo_run.py` records these; do not advertise a latency target as a measurement. [docs/evidence/latest.md](evidence/latest.md) summarizes the best live Meta run (`20261031T083945Z.json`).
- Record the model, actual usage, available credits, and estimated cost. A budget ceiling is not the billed cost.
- Show one meaningful revision (a director's fix or a note that becomes version 2) and one recoverable failure, such as a missing key, an invalid segment range, or SAM finding no subject and the center-crop recovery.
- Verify keyboard navigation, a narrow viewport, refresh during a job, and playback of the downloaded file. These browser checks remain outstanding on a live run.
- Re-render a revised plan and verify that a failure or cancellation preserves the previous verified delivery. Show which revision the available download belongs to.
- Include a rotated phone clip or MOV/WebM input in verification. In the macOS app, verify the native Save dialog and open the saved archive from Finder.
- Separate tests with simulated Meta responses from completed live Meta requests in the submission.

## Explain the engineering

The existing editing engine is the execution foundation. New work adds an editorial workspace and durable jobs around it, plus one Meta model adapter. The important boundary is the validated brief: model output can suggest moments, geometry, cues, and patches, but cannot execute arbitrary commands.

Meta analyzes a smaller MP4 copy while the final render uses the original source. Subject tracking runs on the approved cut, then maps its geometry back into the original displayed frame. This reduces the tracking workload without exporting the analysis proxy. Explain that distinction with an actual run rather than claiming lossless output or unmeasured savings.

The strongest visual demonstration is a creator correcting a story decision, choosing one fitting effect, and obtaining a playable, verified delivery. Show source timestamps and the actual result together so the audience can judge the change. Effects support the story; a 3D creation workspace is outside this product's current scope. [Spatial effects](SPATIAL_EFFECTS.md)

Keep these distinctions visible:

| Claim | Evidence required |
| --- | --- |
| “The model selected these moments.” | A completed live model request and its validated ranges. |
| “The subject stays in frame.” | Real tracking data, the receipts check "auto: subject stays in frame (SAM 3.1)" computed from those boxes against the crop, and visual playback across movement and cuts. |
| “The file is verified.” | A probe of the actual delivered file and its report. |
| “Every cut lands on a sentence.” | The "auto: cuts land on sentence boundaries" check in that version's receipts, computed from the stored transcript sentences (`src/vedit/lint.py`). |
| “The director fixed it.” | A version whose `source` is `director` or `note`, its `applied_patches` list (including skipped ones), and the previous version still downloadable. |
| “Every Meta call is visible.” | The Meta calls panel on a live run, matching `meta-usage.json`. |
| “It saves editing time.” | A timed task comparison with a defined baseline. |
| “It works for customers.” | Actual users completing the workflow and specific feedback. |

## Scope the submission honestly

### Evidence a judge can inspect

These are evidence categories, not a claim about official scoring weights. The [public event page](https://dev.meta.ai/events/global-hackathon), checked October 29, 2026, describes track-specific judging expectations but still says Official Rules will be published; it does not settle eligibility or treatment of pre-existing code.

| Category | Where this repo demonstrates it (real file paths) | What is not proven |
| --- | --- | --- |
| Impact | [src/vedit/studio/static/studio.js](../src/vedit/studio/static/studio.js) implements source upload, editable story ranges, render approval, and delivery downloads. [src/vedit/campaign.py](../src/vedit/campaign.py) connects the approved edit to a video, cover, post copy, and archive. This is an inspectable workflow for Briefcut by Aubric, Inc.. | There is no timed user study and no customer proof in the repo. Time savings, adoption, and engagement are not established by the workflow's existence. |
| Use of Meta AI tooling | Every model Briefcut calls is a Meta model, through [src/vedit/meta.py](../src/vedit/meta.py). Muse Spark: `plan_story` (with transcript sentence boundaries offered as preferred cut points), `review_cut` (Director's notes with structured `checks` and `patches`), `patch_plan` (a typed note to patches), `find_moments`, `plan_shorts`, `script_prompts`, and `translate_cues`. SAM 3.1: `track_subject` and `subject_masks`. Meta Voice Transcribe: `transcribe_audio`, for the transcript and captions. Muse Image: `generate_cover`, `generate_background`, and `generate_image`. Muse Wonder: `review_cut` with `director="wonder"`. Muse Spark and SAM 3.1 together: [verify_find.py](../src/vedit/verify_find.py) and [hero_word.py](../src/vedit/hero_word.py). Muse Spark alone: [emphasis.py](../src/vedit/emphasis.py) (punch words) and [blender_cards.py](../src/vedit/blender_cards.py) (motion cards). Muse Glimmer 30B in private mode: [open_weights.py](../src/vedit/open_weights.py). [src/vedit/campaign.py](../src/vedit/campaign.py) calls them for planning, tracking, captions, covers, review, and the `revise` route. | Live Meta inference was verified on 2026-10-31 for the whole loop on the generated demo clip: Spark plan (contributor mode), SAM 3.1 tracking of "the wooden bowl", Voice Transcribe captions, Muse Image cover, Director's notes with checks and patches, and `--revise director` producing a verified version 2 (`docs/evidence/20261031T070719Z.json`). `--revise note` was verified live as well (`20261031T083945Z.json`, summarized in `latest.md`: 15/15, then 13/15 with the SAM receipt). Auto-refine ran live twice: 12/16 to 16/16 on `workshop.mp4` (`20261031T141719Z-auto-refine.json`) and 9/11 to 11/11 on the 3 s `talk.mp4` (`20261031T153614Z.json`). Spark graded both passes of each run. These runs predate Meta-only: their planning transcripts came from local Whisper. On 2026-11-04, scenario matrix run A5 repeated plan, Voice Transcribe, review, and `revise director` live over MCP (11 of 13, then 13 of 13), and the hero word, punch words, verified find, motion card, Wonder review, and private mode each have a live record. None establishes quality on real speech or across languages. Shorts has no live record. Repeat the loop on the footage you will demo. |
| Demo | [docs/HACKATHON.md](HACKATHON.md) provides the three-minute demonstration sequence; [docs/evidence/latest.md](evidence/latest.md) holds a measured run from `scripts/demo_run.py`. [src/vedit/studio/static/studio.js](../src/vedit/studio/static/studio.js) exposes review, correction, playback, receipts, version tabs, director's fixes, and downloads; [src/vedit/campaign.py](../src/vedit/campaign.py) probes the output, grades it, and writes `report.json` before assembling `campaign.zip`. | A demo script and implementation are not a completed demo run. Inspect the actual delivered video and report; measure latency on that run. The campaign zip is not a published post. |
| Technical depth | [src/vedit/campaign.py](../src/vedit/campaign.py) validates ranges, maps tracking geometry from the analysis proxy to the source, compiles a brief through the `vedit` engine, and renders revisions as new versions. [src/vedit/lint.py](../src/vedit/lint.py) grades every version deterministically and applies patches without I/O. [src/vedit/brief_ops.py](../src/vedit/brief_ops.py) turns editing phrases in the brief into ops. [src/vedit/story.py](../src/vedit/story.py) snaps cuts to transcript sentences. [src/vedit/compile.py](../src/vedit/compile.py) implements `track_reframe` through `_track_reframe` and animated framing through `_spatial_effect`; [src/vedit/effects.py](../src/vedit/effects.py) defines and validates the presets. | Floating frame, Perspective sway, and Push-in reveal are animated framing of a flat video plane, not reconstructed 3D. Geometry validation does not establish tracking quality; that needs real tracking data and playback. Lint checks are measurements against the transcript, not a judgment of the story. Code structure alone does not prove production reliability. |

Every plan comes from Muse Spark, so a live demo needs a key, enabled Meta calls, and credits. Private mode covers only the three text-only steps, so it does not remove the need for a key. A judge without a key can read the recorded runs linked from the README. Do not present a recorded run as a live call.

Live Meta flows were verified on 2026-10-31 and 2026-11-04 (see `docs/SUBMISSION.md` and the dated evidence files). These records exercise server routes and MCP; browser presentation remains unverified on a live Meta API run. The scenario matrix lists 11 bugs it found on real footage; `docs/SUBMISSION.md` says which are fixed. They do not establish transcription quality across languages or on noisy footage. Keep the latest runtime report beside the submission for the exact flows and artifacts that were verified.

Do not claim automated social publishing, multilingual dubbing, a synthetic voice, generative video, guaranteed engagement, on-device video understanding (private mode is text only), or production hosting unless those features have been separately implemented and verified. Keep the product claim narrower than the evidence, and use the demo to make that evidence easy to inspect.
