# Briefcut by Aubric.ai — hackathon submission

**One line.** Briefcut turns one real clip into an editable story and a verified, downloadable campaign: Meta's models propose and track, the local `vedit` compiler cuts, and every claim ships with its proof.

Event: Meta Global AI Developer Hackathon. Submission deadline **November 4, 2026, 9:00 PM ET**. Official event page: <https://dev.meta.ai/events/global-hackathon>. This document is the write-up; keep every claim narrower than the evidence linked from it.

## The problem

Creators and small makers have footage and no time. A phone clip of a product being made sits unused because turning it into a 20-second vertical story means scrubbing, choosing moments, reframing, captioning, exporting, and checking the file. "AI editors" that do this today are cloud black boxes: you cannot see why a cut was chosen, you cannot re-run the result, and you cannot prove the exported file is what you approved.

## What Briefcut does

1. **Upload one clip** (MP4, MOV, WebM, up to 200 MB, up to 20 minutes). The original never leaves the machine unless the creator chooses Meta mode and confirms it.
2. **Describe the outcome** in a sentence: "Open with the finished bowl, then show how it is made."
3. **Get an editable story plan**: source-clock ranges with a reason for each. In Local mode this is deterministic analysis (scene cuts, speech ranges, transcript keywords). In Meta mode, Muse Spark watches the footage and proposes the moments.
4. **Correct it**: move, trim, reorder, add, or delete moments against the source video; edit title and post copy; add one curated spatial effect.
5. **Approve the render**. The approved plan compiles to a YAML brief → `project.json` → `render.sh` → FFmpeg. The job is durable: closing the browser does not stop it.
6. **Verify and download**: the server probes the delivered file (duration, dimensions, streams, bt709/tv color) and refuses to mark the job complete otherwise. The package holds the video, cover, post copy, `story.json`, `brief.yaml`, and `report.json`.

## How Meta's models are used

All Meta calls live in one adapter, `src/vedit/meta.py`, behind `MODEL_API_KEY` and `VEDIT_META_ENABLED=1`. Models return **data**, never commands; every result is schema-validated before the compiler sees it. Each campaign carries a spending cap; the adapter reserves the worst-case cost before each call and keeps the reservation after an uncertain failure. Prices below are from <https://dev.meta.ai/docs/pricing-rate-limits> as of October 30, 2026.

| Capability | Model | Where | What it does in Briefcut |
| --- | --- | --- | --- |
| Story planning | `muse-spark-1.3` (video understanding, strict JSON schema output) | `meta.plan_story`, called from `CampaignManager._plan` in `src/vedit/campaign.py` | Watches a 960×540 analysis proxy of the full clip and returns 1–8 non-overlapping source ranges with visible reasons, title, summary, caption, hashtags, and a cover prompt. Token count is checked with `POST /v1/responses/input_tokens` before the call. $1.25 / $4.25 per million tokens. |
| Subject tracking | `sam-3.1` (video segmentation) | `meta.track_subject`, called from `CampaignManager._render`; geometry mapped by `remap_proxy_points`; applied by `_track_reframe` in `src/vedit/compile.py` | Follows the named subject across the approved cut only, returns per-frame boxes, and drives a smooth 9:16 reframe of the original footage. $0.20 per 1,000 video frames, 50-frame minimum, ≤15,000 frames. |
| Cover image | `muse-image-1.0` (image edit) | `meta.generate_cover`, called from `CampaignManager._render` | Edits a real frame from the delivered video into a PNG cover; opt-in per campaign. $0.01 per image. |
| Captions | `muse-voice-transcribe-1.0` (`POST /v1/asr/transcribe`) | `meta.transcribe_audio`, used by the captions path in `src/vedit/campaign.py` when `caption_provider` is `meta` | Transcribes the approved cut's audio (mono 16-bit WAV, ≤10 min, ≤32 MB) into turn-level cues that are burned in locally. $0.18 per hour of audio. Local Whisper remains the default. |

Local mode makes **no** model calls. Nothing is sent to Meta by opening Studio; a Meta request happens only after the creator picks "Meta director" and confirms the consent checkbox.

## Architecture

```text
browser (src/vedit/studio/static)        local only, 127.0.0.1
   │  upload once · plan · edit · approve · download
   ▼
HTTP server (src/vedit/studio/server.py)
   ▼
CampaignManager (src/vedit/campaign.py)  SQLite job record · one worker · cancel · retry
   ├── Local planner (src/vedit/story.py)        scenes + speech + transcript keywords
   ├── Meta adapter (src/vedit/meta.py)          Muse Spark · SAM 3.1 · Muse Image · Voice Transcribe
   │        budget ledger · schema validation · upload cleanup
   ▼
approved plan → YAML brief → compiler (src/vedit/compile.py) → project.json + render.sh
   ▼
FFmpeg (ffmpeg-full, VideoToolbox on Apple Silicon) → probe → report.json → campaign.zip
```

The boundary that matters: the model proposes moments and geometry; the compiler is the only thing that produces executable steps. A judge can open `brief.yaml` and `render.sh` from any delivery and re-run the edit without Briefcut.

## What is verified, and what is not

| Claim | Evidence | Not established |
| --- | --- | --- |
| The local flow works end to end | `docs/evidence/latest.md`, written by `scripts/demo_run.py` from a real HTTP run: upload → plan → approve → render → probe. | Behavior on other hardware. Run the script on the demo machine the day of submission. |
| The exported file is verified | `report.json` carries the probe of the delivered file; the server refuses completion otherwise. | Editorial quality. A passing probe says the file plays with the right shape and color, not that the story is good. |
| Meta's models are integrated and ran live | `src/vedit/meta.py` with HTTP-fixture tests in `tests/test_meta.py`, plus live runs on 2026-10-31 recorded under `docs/evidence/`: Muse Spark (`muse-spark-1.3-contributor`) planned the workshop clip (`20261031T004918Z.json`, 11,094 in / 2,376 out tokens, $0.0016); Muse Voice Transcribe captioned the approved cut and Muse Image produced the cover (`20261031T005146Z.json`, $0.0127 total); SAM 3.1 tracked "red square" across 300 frames and the reframe kept it centered (`20261031T005236Z.json`, $0.06; frames in `sam-tracking-frames.png`). Total live spend across all runs: $0.17. | Output quality on real-world footage: the live runs used synthetic demo clips. Contributor mode was used for Spark, which permits Meta to train on the submitted clip. Run `scripts/demo_run.py --provider meta` on your own footage before the demo. |
| Subject stays in frame | Geometry remapping and tracking tests; `track_reframe` compiles to a bounded, smoothed crop. | Tracking quality on real footage until a live SAM run is recorded and played back. |
| Spatial effects | `src/vedit/effects.py` presets with validation; rendered locally by `_spatial_effect`. | These animate a flat plane. They are not depth reconstruction. |
| Saves editing time | A 40 s clip reached an editable plan in ~1.5 s and a verified vertical render in ~5.6 s on an Apple M5 Max (see `docs/evidence/latest.md`). | A timed comparison against a human editor; none was run. |

## Demo

The three-minute sequence is in `docs/HACKATHON.md`. Record it on the real build with `./scripts/studio.sh --open`, using `examples/demo/workshop.mp4` (a narrated five-scene clip generated by `uv run python scripts/make_demo.py`) or your own footage. Label any prerecorded or sped-up segment on screen.

## Cost model

- Local edit: $0. FFmpeg, whisper.cpp, and the planner run on the machine.
- Meta mode for a 40 s clip, worst case reserved by the adapter: story planning well under $0.10; tracking 1,200 frames ≈ $0.24; cover $0.01; transcription of 20 s ≈ $0.001. The campaign cap (default $0.25, maximum $5) stops a run before it exceeds the chosen ceiling. The ledger in `meta-usage.json` is a local estimate, not the provider's bill.

## Track and theme

- **Track:** choose one and state it in the form. Indie developer if submitting as an individual or a team of up to four students/builders; Startup if submitting as Aubric.ai with a production story. Judged criteria differ: "creativity, technical ambition, and whether it works" versus "technical execution and whether it holds up in production."
- **Theme:** Build for Your World. Briefcut brings Muse Spark, SAM 3.1, Muse Image, and Voice Transcribe into a maker's editing workflow.
- **Reported judging weights** (secondary sources, not confirmed on the official page as of October 30, 2026): Impact 30%, Use of Meta AI tooling 25%, Demo 25%, Technical depth and execution 20%.

## Submission-day checklist

1. `uv run vedit doctor` passes; `uv run pytest -q` is green; note the commit hash.
2. Export `MODEL_API_KEY`, `VEDIT_META_ENABLED=1`; run one Meta campaign with a subject and a generated cover; keep its `docs/evidence/*.json`, `meta-usage.json`, and the delivered `report.json`.
3. Run `uv run python scripts/demo_run.py` for the local baseline on the demo machine; commit `docs/evidence/`.
4. Record the demo video from a fresh Studio start; show one correction, one effect, the verified report, and the opened archive. Show the measured timings and the Meta ledger from that run.
5. Re-read this file. Remove any claim the recorded run did not demonstrate.
6. Confirm the Official Rules (published on the Cerebral Valley event page before the start) regarding pre-existing code, team size, and submission format. As of October 30, 2026, the public page did not state them; the waiver page did not load for us.
7. Submit before 9:00 PM ET on November 4. Include the repository link, the demo video, this write-up, and the evidence folder.
