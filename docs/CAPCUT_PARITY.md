# CapCut parity

Status of each capability against what CapCut publishes. This file reports measured state, so a row changes only when the feature has been run on real media.

Sources: [CapCut desktop editor](https://www.capcut.com/tools/desktop-video-editor), [CapCut AI video editor](https://www.capcut.com/tools/ai-video-editor), [CapCut AutoCut article](https://www.capcut.com/resource/top-5-autocut-agents-with-automatic-captions), [CapCut Codex page](https://www.capcut.com/uk-ua/features/codex-mcp). Third-party reviews were used only for filler removal, beat sync, and caption translation. CapCut was not installed here, so nothing below is a head-to-head quality test, and a "Match" means the capability exists and works, not that the result is as good.

Status values:

- **Ahead:** CapCut's pages do not describe this.
- **Match:** the capability exists and was run on media.
- **Partial:** it exists with a stated limit.
- **Built:** it exists and passed a browser check, but has not run against live Meta yet.
- **Gap:** it does not exist.

Last updated 2026-11-04 for the Meta-only build. Every model Briefcut calls is now a Meta model: Muse Spark, SAM 3.1, Meta Voice Transcribe, and Muse Image. Some rows were measured on 2026-10-31 with a local Whisper model or the macOS voice, both since removed. Those rows say so. Rows describe capability evidence; they do not establish head-to-head quality.

## Editing

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Multiple clips and photos | Match | 1 to 30 items in order, mixed shapes on a blurred fill. A live two-video project planned by Muse Spark (`evidence/20261031-live-multiclip.json`); mixed-media coverage comes from tests. Broad footage quality is not established. |
| Timeline: trim, split, reorder, ripple delete, snap, undo | Partial | Built in `static/timeline.js`, with undo and redo in the editor core. No browser run is on record. Split is tested at the patch level (`tests/test_director.py`). Needs a recorded browser run. |
| Filmstrip and waveform tracks | Match | Generated at planning, 200 tiles and 4,000 peaks at most. |
| Live preview before render | Partial | Plays cuts, speed, captions, text, and an approximate look. Hard cuts show a one-frame gap. Tracked reframe, cutout, voice cleanup, and spatial effects do not preview. |
| Transcript-based editing | Partial | Edit caption text inline, and cut by deleting words. Word deletion was built and run on word timing from local Whisper, which is removed. Voice Transcribe returns turn-level cues, so word-level cuts on its transcript are unverified. <!-- VERIFY: does word deletion still work in Meta-only mode, and at what granularity? --> |
| Speed per moment | Match | 0.25x to 4x, with audio kept in sync. |
| Keyframes and graphs | Partial | A two-keyframe move per moment. No graph editor. |
| Transitions | Partial | 10 types through FFmpeg `xfade`, against a much larger library. |
| Effects library | Gap | 3 spatial effects and 2 zoom modes, against "massive effects". |
| Templates | Gap | 7 setting bundles, against CapCut's template library. |
| Stickers | Partial | Timed image overlays. No sticker library or AI stickers. |
| Text overlays | Partial | 4 styles with timing and fades. Emoji are removed with a warning. |

## Picture and sound

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Color: looks, sliders, auto adjust | Partial | 10 looks, 6 sliders, and auto adjust that recovered footage two stops under. No color wheel, and auto applies one correction to the whole video. |
| Stabilization | Partial | Reduced synthetic shake from 10.42 to 0.85 px. No clear benefit on the four tested iPhone clips, and slightly worse on a whip pan; opt-in rather than part of the recap template. |
| Noise reduction and voice enhancement | Partial | Noise down about 18 dB on synthetic speech. On low-margin real recordings the gate can cut some speech. |
| Music with ducking | Partial | Your own audio, looped, ducked, and faded. No licensed music library. |
| Beat sync | Partial | Beat detection matched click tracks within 1 ms. Only moments without speech move to the beat. |
| Text to speech | Gap | The macOS voice used before is removed, and no Meta voice model is wired in. <!-- VERIFY: confirm no narration path ships in Meta-only mode, or describe the one that does. --> |

## AI features

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Auto captions | Match | Meta Voice Transcribe. Live: 10 English cues (`evidence/20261031-live-translation.json`), and the captions in `evidence/20261031T083945Z.json`. |
| Animated word captions | Partial | 5 styles. Word timing averaged 46 ms of error on synthetic speech with local Whisper, now removed. Voice Transcribe returns turn-level cues; word timing on its output is unmeasured. <!-- VERIFY: where word timing comes from in Meta-only mode. --> |
| Caption translation | Partial | Muse Spark. Spanish translation and burned captions were live-verified on 2026-10-31 (`evidence/20261031-live-translation.json`: 10 cues in, 10 out). Other language quality is not established. |
| Filler word removal | Partial | Measured with local Whisper, now removed: 33 of 36 fillers set apart by a pause, 2 of 21 said mid-flow. How many fillers Voice Transcribe keeps in its transcript is unmeasured. <!-- VERIFY: does filler removal still run on Voice Transcribe output? --> |
| Pause removal | Match | Decided before the render and shown on the timeline. |
| Long video to many shorts | Partial | Muse Spark plans 2 to 5 sibling projects with distinct moments. Short sources run out of material. Tested against stubbed Meta responses only; no live record yet. |
| Subject-aware reframing | Match | SAM 3.1 follows a subject named in words (`evidence/20261031T083945Z.json`: 452 boxes, 100% coverage). |
| Background removal | Partial | SAM 3.1 masks of a subject named in words, 5 modes. Clean on solid subjects. Flyaway hair is lost and thin gaps keep slivers of background. Live run on a red square with a Muse Image background: SAM masked 100% of 360 frames, 7/7 checks (`evidence/20261031-live-cutout.json`). |
| Search footage by content | Partial | Transcript search runs on this computer over the transcript. Visual search through Muse Spark, tested against stubbed responses only; no live record yet. |
| Script to video | Partial | Muse Spark writes picture prompts, Muse Image makes the stills, and the full editor works afterwards. Stills only, no generated video. Live run: 4 stills, 19.67 s, 5/5 checks (`evidence/20261031-script-to-video.json`); that run was narrated by the macOS voice, now removed. <!-- VERIFY: narration and scene timing in Meta-only mode. --> |
| AI video generation | Gap | The Meta Model API has no video generation model. |
| AI avatars, voice cloning | Gap | Not built. |
| Inpainting | Gap | Muse Image has no mask support. |
| Camera tracking | Gap | Not built. |

## Delivery and platform

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Export options | Match | 720p to 4K, 24 to 60 fps, MP4, MOV, WebM, GIF, and 4 frame shapes. |
| Direct publishing | Gap | Downloads only. |
| Cloud projects and collaboration | Gap | Projects stay on your computer by design. |
| Mobile apps | Gap | The web editor works at phone width. There is no app. |
| Stock media library | Gap | Not built. |

## Where Briefcut goes further

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Model reviews its own render | Ahead | Muse Spark watches the result and returns structured fixes. Auto-refine took a cut from 12 of 16 checks to 16 of 16 (`evidence/20261031T141719Z-auto-refine.json`). Spark graded both versions. |
| Edit by note | Ahead | Typed notes became verified new versions in live HTTP runs: two trims snapped to a sentence end, 15/15 then 13/15 (`evidence/20261031T083945Z.json`); a warmer look, bigger captions, and a slower moment, 14/14 (`evidence/20261031T100101Z-note.json`). |
| Verified versions | Ahead | Each version has a probe of the delivered file and pass or fail receipts with jump-to-time. |
| Re-runnable edit | Ahead | `render.sh` reproduces the edit with FFmpeg alone, and `brief.yaml` recompiles it. |
| Grading the AI's own work | Ahead | Receipts measure SAM's track and cutout, and lint re-checks every model patch. |
| Evidence labels on every check | Built | Each check says how it is known: Probed, Measured (computed by code), or Model opinion (Spark's review). Awaiting a live Meta run. <!-- VERIFY: confirm "evidence labels" means these receipt tags. --> |
| Every model call visible | Built | Studio's Meta calls panel lists each call with model id, purpose, duration, cost, and request id. Awaiting a live Meta run. <!-- VERIFY: panel name, location, and columns. --> |
| Documented agent interface | Ahead | HTTP API and an MCP server any MCP agent can use. CapCut describes a Codex workflow, but no public API documentation was found. |

## Bottom line

Briefcut matches CapCut on the core work of cutting, captioning, and reframing, and goes further on review, verification, and reproducibility. Every model it calls is a Meta model, and Studio shows each call. The editing timeline and the Meta calls panel still need a recorded live browser run. Removing the local speech model left word-level timing and text to speech to re-verify. It is behind on libraries, generated media, platforms, and polish, and most of those gaps cannot be closed by code alone.
