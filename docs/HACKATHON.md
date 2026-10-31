# Briefcut by Aubric.ai demo

The product promise is simple: turn real footage into a story worth watching. Demonstrate the creator inspecting, revising, and exporting that story. Use **Briefcut by Aubric.ai** as the product attribution; `vedit` remains the engine and command-line interface. See [Brand](BRAND.md).

The target is the Meta Global AI Developer Hackathon. Its published submission cutoff is **November 4, 2026, at 9:00 PM ET**. Applications closed October 25. Confirm the team's acceptance, track, and the current rules governing pre-existing code before submitting this repository. This document does not establish eligibility. [Official event details](https://dev.meta.ai/events/global-hackathon) · [Submission write-up](SUBMISSION.md)

Final-project judging weights **as reported by secondary coverage** (not shown on the official page as of October 30, 2026): Impact 30%, Use of Meta AI tooling 25%, Demo 25%, Technical depth and execution 20%. The official page states track criteria without weights: Indie developer is "judged on creativity, technical ambition, and whether it works"; Startup is "judged on technical execution and whether it holds up in production." Themes are "Build for Your World" and "Push the Frontier." Official Rules were announced for publication on the Cerebral Valley event page before October 26; the public page did not show them when checked, and the waiver page did not load. Read them before submitting.

Demo clip: `examples/demo/workshop.mp4` is stylised computer-generated footage, not a recording. `uv run python scripts/make_demo.py` builds five 8 s scenes of an oak bowl on a workbench (finished bowl, raw oak blank, turning with gouge and shavings, sanding, oil finish) from FFmpeg filters with film grain and a vignette, no baked-in text, and narration from the macOS `say` voice (Samantha) with one-second pauses between sentences. It gives scene, silence, transcript, and subject-tracking analysis real structure without customer footage. Say on screen that it is generated. Prefer your own footage for the recording when you have permission.

## Three-minute demonstration

| Time | Show | Explain |
| --- | --- | --- |
| 0:00–0:15 | The maker's clip, including the slow beginning and the useful action. State that the demo clip is generated footage if you use `workshop.mp4`. | Name the person and the editing task they need to finish: a 25-second vertical story. |
| 0:15–0:35 | Upload the clip. Pick the "Product story, edited" chip, or type a brief with an edit instruction: "Make a 25-second vertical story: open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end." Choose Meta director, the consent box, and "Ask Meta to review the finished cut". | The brief is the source of truth. Editing phrases become engine ops and appear as decisions on the plan. The creator chooses what may leave the machine. |
| 0:35–1:00 | Show the Meta director plan: each moment's reason, the scrubber ticks, and that every cut sits on a sentence boundary. Jump to one moment and correct it. | Muse Spark proposed the moments with the transcript and sentence boundaries as data; the local snap finished the sentences. The user can inspect and correct every moment. |
| 1:00–1:20 | Add one effect (Floating frame reads best on a vertical) and name the subject ("the wooden bowl"). | Curated effects render locally over a blurred copy of the footage. SAM 3.1 supplies geometry for the 9:16 reframe, and the receipts grade that track ("subject stays in frame"). If the director finds nothing to fix, type a note instead; it becomes patches through Muse Spark. |
| 1:20–1:50 | Start the render; show its real stages (tracking, transcribing, encoding, reviewing, cover); play the finished short. | Encoding runs locally; the Meta stages are the slow part. The program and report remain available. |
| 1:50–2:15 | Open "The receipts": the probe, the automatic checks, the director's checks with jump-to-time. Read the Director's notes and the one failing check. | Checks are measurements plus the model's opinion, labeled separately. A verified file is not a good story. |
| 2:15–2:40 | Click "Apply director's fixes". Show the v2 tab appear, the "What changed in v2" list (including a skipped patch, if any), and that v1 is still downloadable. | The model proposed patches; the pure patcher applied them; lint re-ran; the compiler rendered. Nothing from the model executed. |
| 2:40–2:55 | Download the campaign archive with the Muse Image cover. Open it. | The output is ready for a creator's publishing workflow. It has not been posted automatically. |
| 2:55–3:00 | Show the Run facts: time to plan, time to render, reserved spend from this run. | Explain the observed benefit and the limits plainly. |

Adjust the recording to actual render time; the live Meta loop took about 68 s to plan and 290 s to render v1 on the recorded run (`docs/evidence/latest.md`), so cut or label the wait. If a stage is sped up or a result was prepared earlier, label it. A prerecorded result can support a live demo but must not impersonate a live model call.

## Prepare evidence

- Use footage you own or have permission to demonstrate. Select a clip where the desired story and subject are visually obvious.
- Restart the current application build, read its startup output, and run the entire flow from a fresh source upload.
- Start the checkout with `./scripts/studio.sh --open` so the locally installed caption model (shared `~/.vedit/models/`, or the checkout's `.vedit-models/`) is discovered. The launcher creates the environment with `uv` if `.venv` is missing. A packaged app needs its own external dependencies and caption-model environment.
- Run `uv run python scripts/demo_run.py` on the demo machine. It drives the real HTTP flow (upload, plan, approve, render, probe, checks) and writes `docs/evidence/<timestamp>.json` and `docs/evidence/latest.md` with measured time to plan, time to render, machine details, the probe of the downloaded file, the receipts, and the version table. Add `--provider meta --subject "the wooden bowl" --caption-provider meta --generate-cover --review --revise director --budget 0.5` once credentials are configured, and keep that run's record beside the submission. `--revise auto` and `--revise note --note "..."` exercise the other two revision modes.
- Save the source, approved plan, generated program, output, verification report, and relevant test results.
- Record time to first useful plan, time to completed video, input duration/resolution, and the hardware used. `scripts/demo_run.py` records these; do not advertise a latency target as a measurement. The first recorded local run is summarized in [docs/evidence/latest.md](evidence/latest.md).
- For cloud work, record the model, actual usage, available credits, and estimated cost. A budget ceiling is not the billed cost.
- Show one meaningful revision (a director's fix or a note that becomes version 2) and one recoverable failure, such as missing cloud credentials, an invalid segment range, or SAM finding no subject and the center-crop recovery.
- Verify keyboard navigation, a narrow viewport, refresh during a job, and playback of the downloaded file.
- Re-render a revised plan and verify that a failure or cancellation preserves the previous verified delivery. Show which revision the available download belongs to.
- Include a rotated phone clip or MOV/WebM input in verification. In the macOS app, verify the native Save dialog and open the saved archive from Finder.
- Separate tested local behavior, provider tests with simulated HTTP responses, and completed live Meta requests in the submission.

## Explain the engineering

The existing editing engine is the execution foundation. New work adds an editorial workspace and durable jobs around it, plus optional model adapters. The important boundary is the validated brief: model output can suggest moments and geometry, but cannot execute arbitrary commands.

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
| “It saves editing time.” | A timed task comparison with a defined baseline. |
| “It works for customers.” | Actual users completing the workflow and specific feedback. |

## Scope the submission honestly

### Evidence a judge can inspect

These are evidence categories, not a claim about official scoring weights. The [public event page](https://dev.meta.ai/events/global-hackathon), checked October 29, 2026, describes track-specific judging expectations but still says Official Rules will be published; it does not settle eligibility or treatment of pre-existing code.

| Category | Where this repo demonstrates it (real file paths) | What is not proven |
| --- | --- | --- |
| Impact | [src/vedit/studio/static/studio.js](../src/vedit/studio/static/studio.js) implements source upload, editable story ranges, render approval, and delivery downloads. [src/vedit/campaign.py](../src/vedit/campaign.py) connects the approved edit to a video, cover, post copy, and archive. This is an inspectable workflow for Briefcut by Aubric.ai. | There is no timed user study and no customer proof in the repo. Time savings, adoption, and engagement are not established by the workflow's existence. |
| Use of Meta AI tooling | [src/vedit/meta.py](../src/vedit/meta.py): `plan_story` uses Muse Spark (with transcript sentence boundaries offered as preferred cut points), `review_cut` asks Spark for Director's notes with structured `checks` and `patches`, `patch_plan` turns a typed director's note into patches, `track_subject` uses SAM 3.1, `generate_cover` uses Muse Image, and `transcribe_audio` uses Muse Voice Transcribe for opt-in captions. These are opt-in code paths behind `MODEL_API_KEY` and `VEDIT_META_ENABLED=1`; [src/vedit/campaign.py](../src/vedit/campaign.py) calls them for planning, tracking, captions, covers, review, and the `revise` route. | Live Meta inference was verified on 2026-10-31 for the whole loop on the generated demo clip: Spark plan (contributor mode), SAM 3.1 tracking of "the wooden bowl", Voice Transcribe captions, Muse Image cover, Director's notes with checks and patches, and `--revise director` producing a verified version 2 (`docs/evidence/20261031T070719Z.json`, `latest.md`). Earlier records under `docs/evidence/` and the SAM contact sheet `sam-tracking-frames.png` cover the first adapter checks. These runs do not establish quality on real footage or across languages; `--revise note` was verified live as well (`20261031T083945Z.json`, 15/15 then 13/15 with the SAM receipt); `auto` is verified by tests. Repeat the loop on the footage you will demo. |
| Demo | [docs/HACKATHON.md](HACKATHON.md) provides the three-minute demonstration sequence; [docs/evidence/latest.md](evidence/latest.md) holds a measured run from `scripts/demo_run.py`. [src/vedit/studio/static/studio.js](../src/vedit/studio/static/studio.js) exposes review, correction, playback, receipts, version tabs, director's fixes, and downloads; [src/vedit/campaign.py](../src/vedit/campaign.py) probes the output, grades it, and writes `report.json` before assembling `campaign.zip`. | A demo script and implementation are not a completed demo run. Inspect the actual delivered video and report; measure latency on that run. The campaign zip is not a published post. |
| Technical depth | [src/vedit/campaign.py](../src/vedit/campaign.py) validates ranges, maps tracking geometry from the analysis proxy to the source, compiles a brief through the `vedit` engine, and renders revisions as new versions. [src/vedit/lint.py](../src/vedit/lint.py) grades every version deterministically and applies patches without I/O. [src/vedit/brief_ops.py](../src/vedit/brief_ops.py) turns editing phrases in the brief into ops. [src/vedit/story.py](../src/vedit/story.py) snaps cuts to transcript sentences. [src/vedit/compile.py](../src/vedit/compile.py) implements `track_reframe` through `_track_reframe` and animated framing through `_spatial_effect`; [src/vedit/effects.py](../src/vedit/effects.py) defines and validates the presets. | Floating frame, Perspective sway, and Push-in reveal are animated framing of a flat video plane, not reconstructed 3D. Geometry validation does not establish tracking quality; that needs real tracking data and playback. Lint checks are measurements against the transcript, not a judgment of the story. Code structure alone does not prove production reliability. |

Local mode proposes moments from scene cuts, speech ranges, and transcript sentences, spread across scenes for process briefs ([src/vedit/story.py](../src/vedit/story.py)); label it as local analysis, not model understanding. The local workflow can be demonstrated without a Meta account. Meta credentials, enabled cloud access, sufficient credits, and a successful live run are required to demonstrate Meta inference. Do not label a deterministic local plan as AI understanding.

Live Meta inference was verified in this build on 2026-10-31 (see the table above and `docs/evidence/latest.md`). Local speech transcription has been exercised with real audio from the generated clip; neither establishes transcription quality across languages or on noisy footage. Keep the latest runtime report beside the submission for the exact flows and artifacts that were verified.

Do not claim automated social publishing, multilingual dubbing, generative video, guaranteed engagement, or production hosting unless those features have been separately implemented and verified. Keep the product claim narrower than the evidence, and use the demo to make that evidence easy to inspect.
