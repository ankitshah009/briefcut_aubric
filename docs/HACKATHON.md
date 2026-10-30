# Briefcut by Aubric.ai demo

The product promise is simple: turn real footage into a story worth watching. Demonstrate the creator inspecting, revising, and exporting that story. Use **Briefcut by Aubric.ai** as the product attribution; `vedit` remains the engine and command-line interface. See [Brand](BRAND.md).

The likely target is the Meta Global AI Developer Hackathon. Its published submission cutoff is **November 4, 2026, at 9:00 PM ET**. Applications closed October 25. Confirm the team's acceptance, track, and the current rules governing pre-existing code before submitting this repository. This document does not establish eligibility. [Official event details](https://dev.meta.ai/events/global-hackathon)

## Three-minute demonstration

| Time | Show | Explain |
| --- | --- | --- |
| 0:00–0:20 | A real product demonstration or maker's clip, including the slow beginning and the useful action. | Name the person and the specific editing task they need to finish. |
| 0:20–0:45 | Upload the clip and enter a concrete brief: “Make a 20-second vertical story: show the finished object, then how it is made.” | The creator describes an outcome and chooses what may leave the machine. |
| 0:45–1:15 | Review the proposed source ranges; jump to their timestamps and change one cut. | Meta can propose the story when configured. The user can inspect and correct every selected moment. Label Local plans accurately if using the local mode. |
| 1:15–1:40 | Add one deliberate visual accent; when live Meta tracking is available, also show the subject-following result. | Floating frame, Perspective sway, and Push-in reveal are curated effects inside the video editor. SAM supplies geometry for framing. Show the actual rendered result. |
| 1:40–2:20 | Start the approved render, show its real stages, and play the finished short. | Encoding runs locally. The program and verification report remain available. |
| 2:20–2:45 | Download the video, cover, post copy, and delivery archive. Open the archive. | The output is ready for a creator's publishing workflow. It has not been posted automatically. |
| 2:45–3:00 | Show measured latency and cost from this run, then the next concrete product milestone. | Explain the observed benefit and the limits plainly. |

Adjust the recording to actual render time. If a stage is sped up or a result was prepared earlier, label it. A prerecorded result can support a live demo but must not impersonate a live model call.

## Prepare evidence

- Use footage you own or have permission to demonstrate. Select a clip where the desired story and subject are visually obvious.
- Restart the current application build, read its startup output, and run the entire flow from a fresh source upload.
- Start the checkout with `./scripts/studio.sh --open` so the locally installed caption model is discovered. A packaged app needs its own external dependencies and caption-model environment.
- Save the source, approved plan, generated program, output, verification report, and relevant test results.
- Record time to first useful plan, time to completed video, input duration/resolution, and the hardware used. Do not advertise a latency target as a measurement.
- For cloud work, record the model, actual usage, available credits, and estimated cost. A budget ceiling is not the billed cost.
- Show one meaningful revision and one recoverable failure, such as missing cloud credentials or an invalid segment range.
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
| “The subject stays in frame.” | Real tracking data and visual playback across movement and cuts. |
| “The file is verified.” | A probe of the actual delivered file and its report. |
| “It saves editing time.” | A timed task comparison with a defined baseline. |
| “It works for customers.” | Actual users completing the workflow and specific feedback. |

## Scope the submission honestly

### Evidence a judge can inspect

These are evidence categories, not a claim about official scoring weights. The [public event page](https://dev.meta.ai/events/global-hackathon), checked October 29, 2026, describes track-specific judging expectations but still says Official Rules will be published; it does not settle eligibility or treatment of pre-existing code.

| Category | Where this repo demonstrates it (real file paths) | What is not proven |
| --- | --- | --- |
| Impact | [src/vedit/studio/static/studio.js](../src/vedit/studio/static/studio.js) implements source upload, editable story ranges, render approval, and delivery downloads. [src/vedit/campaign.py](../src/vedit/campaign.py) connects the approved edit to a video, cover, post copy, and archive. This is an inspectable workflow for Briefcut by Aubric.ai. | There is no timed user study and no customer proof in the repo. Time savings, adoption, and engagement are not established by the workflow's existence. |
| Use of Meta AI tooling | [src/vedit/meta.py](../src/vedit/meta.py): `plan_story` uses Muse Spark, `track_subject` uses SAM 3.1, and `generate_cover` uses Muse Image. These are opt-in code paths behind `MODEL_API_KEY` and `VEDIT_META_ENABLED=1`; [src/vedit/campaign.py](../src/vedit/campaign.py) calls them for story planning, requested subject tracking, and requested generated covers. | Live Meta inference has not been verified in this session. Configured credentials and implemented adapters do not prove successful inference or output quality. Local deterministic plans are not model understanding. |
| Demo | [docs/HACKATHON.md](HACKATHON.md) provides the three-minute demonstration sequence. [src/vedit/studio/static/studio.js](../src/vedit/studio/static/studio.js) exposes review, revision, playback, and downloads; [src/vedit/campaign.py](../src/vedit/campaign.py) probes the output and writes `report.json` before assembling `campaign.zip`. | A demo script and implementation are not a completed demo run. Inspect the actual delivered video and report; measure latency on that run. The campaign zip is not a published post. |
| Technical depth | [src/vedit/campaign.py](../src/vedit/campaign.py) validates ranges, maps tracking geometry from the analysis proxy to the source, and compiles a brief through the `vedit` engine. [src/vedit/compile.py](../src/vedit/compile.py) implements `track_reframe` through `_track_reframe` and animated framing through `_spatial_effect`; [src/vedit/effects.py](../src/vedit/effects.py) defines and validates the presets. | Floating frame, Perspective sway, and Push-in reveal are animated framing of a flat video plane, not reconstructed 3D. Geometry validation does not establish tracking quality; that needs real tracking data and playback. Code structure alone does not prove production reliability. |

The local workflow can be demonstrated without a Meta account. Meta credentials, enabled cloud access, sufficient credits, and a successful live run are required to demonstrate Meta inference. Do not label a deterministic local plan as AI understanding.

Live Meta inference has not been verified in the current build session. Local speech transcription has been exercised with real audio; that does not establish live Meta behavior or transcription quality across languages. Keep the latest runtime report beside the submission for the exact flows and artifacts that were verified.

Do not claim automated social publishing, multilingual dubbing, generative video, guaranteed engagement, or production hosting unless those features have been separately implemented and verified. Keep the product claim narrower than the evidence, and use the demo to make that evidence easy to inspect.
