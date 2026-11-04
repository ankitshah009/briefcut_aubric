# CapCut parity

Status of each capability against what CapCut publishes. This file reports measured state, so a row changes only when the feature has been run on real media.

Sources: [CapCut desktop editor](https://www.capcut.com/tools/desktop-video-editor), [CapCut AI video editor](https://www.capcut.com/tools/ai-video-editor), [CapCut AutoCut article](https://www.capcut.com/resource/top-5-autocut-agents-with-automatic-captions), [CapCut Codex page](https://www.capcut.com/uk-ua/features/codex-mcp). Third-party reviews were used only for filler removal, beat sync, and caption translation. CapCut was not installed here, so nothing below is a head-to-head quality test, and a "Match" means the capability exists and works, not that the result is as good.

Status values:

- **Ahead:** CapCut's pages do not describe this.
- **Match:** the capability exists and was run on media.
- **Partial:** it exists with a stated limit.
- **Gap:** it does not exist.

Last updated 2026-11-04 for the Meta-only build. Every model Briefcut calls is a Meta model: Muse Spark, Muse Wonder, SAM 3.1, Meta Voice Transcribe, and Muse Image on the Meta Model API, and Muse Glimmer 30B on this computer in private mode. Some rows were measured on 2026-10-31 with a local Whisper model or the macOS voice, both since removed. Those rows say so. The [scenario matrix](evidence/scenario-matrix/README.md) ran 14 renders on footage Briefcut was not built around and found 11 bugs; rows it affects say so. Rows describe capability evidence; they do not establish head-to-head quality.

## Editing

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Multiple clips and photos | Partial | 1 to 30 items in order, mixed shapes on a blurred fill. Live two-video projects planned by Muse Spark (`evidence/20261031-live-multiclip.json`, scenario matrix A4), and six mixed iPhone items (rotated HLG, HEIC, JPG, VFR) with Meta stubbed (B3). A moment can cross a clip boundary and flash 13 frames of the next clip (bug 3), and one render's audio ran 0.07 s past its video (bug 9). |
| Timeline: trim, split, reorder, ripple delete, snap, undo | Partial | Built in `static/timeline.js`, with undo and redo in the editor core. No browser run is on record. Split is tested at the patch level (`tests/test_director.py`). Needs a recorded browser run. |
| Filmstrip and waveform tracks | Match | Generated at planning, 200 tiles and 4,000 peaks at most. |
| Live preview before render | Partial | Plays cuts, speed, captions, text, and an approximate look. Hard cuts show a one-frame gap. Tracked reframe, cutout, voice cleanup, and spatial effects do not preview. |
| Transcript-based editing | Partial | Edit caption text inline, and select words to cut them. Voice Transcribe times turns, not words, so word times are spread across each turn by length and marked estimated. A cut that touches estimated words widens to the whole sentence (`sentenceRanges` in `timeline.js`), so Briefcut never cuts audio at a guessed word time. Exact word-level cuts need word timing, which Meta Voice Transcribe does not report. |
| Speed per moment | Match | 0.25x to 4x, with audio kept in sync. |
| Keyframes and graphs | Partial | A two-keyframe move per moment. No graph editor. |
| Transitions | Partial | 10 types through FFmpeg `xfade`, against a much larger library. |
| Effects library | Gap | 3 spatial effects, 2 zoom modes, a hero word behind the subject, and 3D motion cards, against "massive effects". |
| Templates | Gap | 7 setting bundles, against CapCut's template library. |
| 3D titles | Partial | Muse Spark designs a motion card from closed choices; Blender renders Briefcut's own template on this computer. Live: a 3 s intro card over a 1080×1920 render, 90 frames in 24 s (`evidence/blender-cards/README.md`). Needs Blender 4.2 or later, and is hidden without it. 5 styles. |
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
| Text to speech | Gap | Meta has no text-to-speech model, so Briefcut has no voice. The macOS voice used before is removed, and script to video is caption-led. |

## AI features

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Auto captions | Match | Meta Voice Transcribe, heard once at planning and reused for the captions. The spoken language defaults to "Detect automatically", or the user picks one of 25. Live: 10 English cues (`evidence/20261031-live-translation.json`), the captions in `evidence/20261031T083945Z.json`, and scenario matrix A5 and C on 2026-11-04. Repeated phrases such as "the bowl the bowl" were burned in during that run (bug 8); they are now collapsed. No live run on a real person's speech. |
| Animated word captions | Partial | 5 styles, and punch words: Muse Spark picks the word that carries a line and the caption draws it larger in an accent color (`evidence/caption-emphasis/README.md`). Word timing is estimated from Voice Transcribe's turns by length, so a word-by-word highlight is approximate. The 46 ms error measured earlier was local Whisper on synthetic speech, now removed. |
| Caption translation | Partial | Muse Spark. Spanish translation and burned captions were live-verified on 2026-10-31 (`evidence/20261031-live-translation.json`: 10 cues in, 10 out). Other language quality is not established. |
| Filler word removal | Gap | Voice Transcribe gives no word times, so filler removal has nothing to cut at and does nothing. Studio says so on the plan. It worked with local Whisper (33 of 36 fillers set apart by a pause, 2 of 21 said mid-flow), which is removed. |
| Pause removal | Match | Decided before the render and shown on the timeline. |
| Long video to many shorts | Partial | Muse Spark plans 2 to 5 sibling projects with distinct moments. Short sources run out of material. Tested against stubbed Meta responses only; no live record yet. |
| Subject-aware reframing | Match | SAM 3.1 follows a subject named in words (`evidence/20261031T083945Z.json`: 452 boxes, 100% coverage; scenario matrix A1). Black borders inside the source are not detected, so a letterboxed source can leave most of a 9:16 frame black (bug 2). |
| Background removal | Partial | SAM 3.1 masks of a subject named in words, 5 modes. Clean on solid subjects. Flyaway hair is lost and thin gaps keep slivers of background. Live run on a red square with a Muse Image background: SAM masked 100% of 360 frames, 7/7 checks (`evidence/20261031-live-cutout.json`). |
| Search footage by content | Match | Transcript search runs on this computer. Visual search through Muse Spark, then SAM 3.1 checks each found moment is on screen and marks it verified or flagged. Live: the bowl verified in 100% of sampled frames; a stylised gouge flagged by a SAM false negative (`evidence/verified-find/README.md`); a man in a suit verified 45 of 45 frames (scenario matrix A3). CapCut's pages do not describe a second model checking a search; that part goes further. |
| Script to video | Partial | Muse Spark (or Muse Glimmer in private mode) writes picture prompts, Muse Image makes the stills, and the full editor works afterwards. Caption-led: the script's words are timed evenly across each scene, with no voice. Stills only, no generated video. Live run: 4 stills, 19.67 s, 5/5 checks (`evidence/20261031-script-to-video.json`); that run still had the macOS voice, now removed. No live record of the caption-led version. |
| AI video generation | Gap | The Meta Model API has no video generation model. |
| AI avatars, voice cloning | Gap | Not built. |
| Inpainting | Gap | Muse Image has no mask support. |
| Camera tracking | Gap | Not built. |

## Delivery and platform

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Export options | Match | 720p to 4K; the source's own frame rate by default ("Match source"), or 24, 30, or 60 fps; MP4, MOV, WebM, GIF; and 4 frame shapes. |
| Direct publishing | Gap | Downloads only. |
| Cloud projects and collaboration | Gap | Projects stay on your computer by design. |
| Mobile apps | Gap | The web editor works at phone width. There is no app. |
| Stock media library | Gap | Not built. |

## Where Briefcut goes further

| Capability | Status | State in Briefcut |
| --- | --- | --- |
| Model reviews its own render | Ahead | Muse Spark watches the result and returns structured fixes. Auto-refine took a cut from 12 of 16 checks to 16 of 16 (`evidence/20261031T141719Z-auto-refine.json`). Spark graded both versions. An automatic fix can drop the ending a brief asked for and still pass (bug 4). |
| Reviewer that listens | Ahead | Muse Wonder reviews the cut by eye and ear. On one cut it failed two checks the frames confirm and described the soundtrack truthfully where Muse Spark invented a lathe sound (`evidence/wonder-review/README.md`). One clip, one run. Needs a project budget of at least $0.33. |
| Hero word behind the subject | Ahead | Muse Spark picks a spoken word, code checks it is said in one shot, and SAM 3.1's mask puts it behind the subject. The mask overruled Spark's placement where the subject would have covered 82% of the word (`evidence/hero-word/README.md`). One static subject. |
| Edit by note | Ahead | Typed notes became verified new versions in live HTTP runs: two trims snapped to a sentence end, 15/15 then 13/15 (`evidence/20261031T083945Z.json`); a warmer look, bigger captions, and a slower moment, 14/14 (`evidence/20261031T100101Z-note.json`). |
| Verified versions | Ahead | Each version has a probe of the delivered file and pass or fail receipts with jump-to-time. |
| Re-runnable edit | Ahead | `render.sh` reproduces the edit with FFmpeg alone, and `brief.yaml` recompiles it. |
| Grading the AI's own work | Ahead | Receipts measure SAM's track and cutout, and lint re-checks every model patch. |
| Evidence labels on every check | Ahead | Each check says how it is known: Probed, Measured (computed by code), or Model opinion (a reviewer's judgment). Every check in the 2026-11-04 live receipts carries its `basis` (scenario matrix records). Studio's display of the tags has no browser record. |
| Every model call visible | Ahead | Studio's Meta calls panel lists each call live: purpose, status, model, cost, duration, size, and Meta's response id. Seen in a browser on a real project with two private-mode calls made live (`evidence/open-weights/studio-meta-calls-*.png`). No Meta API call has been watched live in it yet. |
| Private mode | Ahead | The director's note, caption translation, and script scenes can run on Muse Glimmer 30B on this computer, so that text never leaves it (`evidence/open-weights/README.md`). It fails closed. Everything else still uses the Meta Model API. |
| Documented agent interface | Ahead | HTTP API and an MCP server with 21 tools that any MCP agent can use, including Meta's Muse Code (`evidence/muse-code/README.md`) and the exact `.mcp.json` command (scenario matrix A). CapCut describes a Codex workflow, but no public API documentation was found. |

## Bottom line

Briefcut matches CapCut on the core work of cutting, captioning, and reframing, and goes further on review, verification, and reproducibility: two Meta models check each other, a reviewer can listen, and Studio shows every call. The editing timeline and a live Meta API call in the panel still need a recorded browser run. Going Meta-only cost word-level timing, filler removal, and any voice. The scenario matrix found real-footage bugs that remain open, listed in `SUBMISSION.md`. It is behind on libraries, generated media, platforms, and polish, and most of those gaps cannot be closed by code alone.
