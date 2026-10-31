# Latest measured run

Recorded 2026-10-31T08:39:45+00:00 by `scripts/demo_run.py`. One run, one machine. Timings are wall-clock and include HTTP polling. Re-run with `uv run python scripts/demo_run.py` to refresh.

## Machine

| Item | Value |
| --- | --- |
| OS | Darwin 27.0.1 (arm64) |
| CPU | Apple M5 Max |
| Memory | 128 GiB |
| Python | Python 3.13.16 |
| FFmpeg | ffmpeg version 9.0.2 Copyright (c) 2000-2026 the FFmpeg developers |
| vedit | 1.2.0 |

## Request

| Item | Value |
| --- | --- |
| Clip | `workshop.mp4` |
| Source | 40.02 s, 1280×720, audio=True |
| Provider | meta |
| Target | 25 s, 9:16 |
| Captions | True (meta) |
| Effect | {'preset': 'float', 'intensity': 0.55, 'start': 0, 'duration': 3} |
| Subject tracking | the wooden bowl |

## Timings

| Stage | Seconds |
| --- | --- |
| Upload | 0.010 s |
| Time to editable plan | 63.790 s |
| Time to verified render | 120.433 s |
| Total | 184.233 s |

## Plan

Provider `meta`, analysis `meta`. 5 segment(s), 21.563 s.

| # | Source range | Label | Reason |
| --- | --- | --- | --- |
| 1 | 0.00–4.58 s | finished oak bowl on table | Top-down view of smooth finished bowl with concentric grain on dark planks is visible throughout; speech [0.0-5.0] states 'This is the finished oak bowl, oiled and ready for the table.'; end moved back to sentence boundary at 4.6 s. |
| 2 | 8.00–12.00 s | raw oak blank close-up | Blurry close-up of rough wood cross-section with wavy growth rings is visible; speech [8.0-12.0] states 'Here is the raw oak blank, still wet and heavy.' |
| 3 | 16.00–20.00 s | bowl shape forming on lathe | Bowl with tool at rim and shavings visible as shape is cut; speech [16.0-20.0] states 'On the lathe the shape appears in long, steady cuts.' |
| 4 | 24.00–29.00 s | sanding the turning bowl | Continued turning view with tool contact and surface refinement visible; speech [24.0-29.0] states 'Sanding goes from coarse to fine, with the lathe turning slowly.' |
| 5 | 32.00–35.98 s | oiled surface detail | Tool removed, clean bowl with warm glowing grain visible top-down; speech [32.0-36.0] states 'A coat of food safe oil brings out the color.' |

## Verified output (version 2)

| Item | Server proof | Local probe of the downloaded file |
| --- | --- | --- |
| Duration | 21.966667 | 21.966667 |
| Size | 1080×1920 | 1080×1920 |
| Audio / video | True / True | True / True |
| Codec | — | h264 @ 30.0 fps |
| Color | bt709 / tv (bt709/bt709) | not reported by `vedit probe`; see report.json |
| Bytes | 4501627 | 4501627 |

Meta spend reserved: $0.2882 of a $0.50 cap. Local mode makes no model calls.

Warnings from the render: none

## Receipts (checks on the first verified render)

15 of 15 checks passed.

| Check | Result | At | Note |
| --- | --- | --- | --- |
| auto: cuts land on sentence boundaries | pass | — | every cut sits on a sentence boundary |
| auto: duration matches the target | pass | — | 21.6 s cut vs 25.0 s target |
| auto: moments inside the source and in order | pass | — | 5 moments inside the source, no overlaps |
| auto: captions cover the speech | pass | — | 95% of 15.1 s of speech has a caption |
| auto: requested edits applied | pass | — | The brief asked for no specific edits. |
| auto: subject stays in frame (SAM 3.1) | pass | — | 452 tracked boxes cover 100% of 21.6 s; longest gap 0.2 s; subject centre inside the 9:16 crop in 100% of boxes |
| director: Vertical format | pass | 1.5 s | Center vertical column with side bars reads as vertical story. |
| director: Story about the oak bowl | pass | 2.0 s | All visuals and captions stay on the oak bowl from finish to making. |
| director: Opens with finished bowl | pass | 0.0 s | Finished bowl on table plays 0.00-4.58s as briefed. |
| director: Shows how it is made after opening | pass | 9.5 s | Blank at 4.58s, lathe forming at 8.59s, sanding at 12.59s, oiled detail at 17.59s. |
| director: Runtime within 25s ceiling | pass | 21.6 s | 21.6s cut is under 25s ceiling with complete sentences. |
| director: Hook inside first 3 seconds | pass | 0.5 s | Finished bowl grain fills frame instantly with clear caption. |
| director: No mid-sentence cuts | pass | — | All five captions play as complete sentences with no truncation. |
| director: Pacing holds for short story | pass | 8.6 s | Even ~4s beats move blank-lathe-sand-oil without drag. |
| director: Caption legibility | pass | 2.1 s | White bottom captions stay legible over dark wood throughout. |

## Self-correcting loop

Mode `note` with note: "Hold the finished bowl a beat longer before the first cut, then tighten the sanding moment.". Version 2 rendered in 130.492 s and passed 13 of 15 checks.

- trim_end moment 1: {'start': 0.0, 'end': 4.585} → {'start': 0.0, 'end': 5.58} — hold the finished bowl a beat longer before the first cut
- trim_end moment 4: {'start': 24.0, 'end': 29.0} → {'start': 24.0, 'end': 28.0} — tighten the sanding moment
- trim_end moment 4: {'start': 24.0, 'end': 28.0} → {'start': 24.0, 'end': 28.363} — Moment 4 ends mid-sentence at 28.00 s; snap to the sentence boundary at 28.36 s.

## Versions

| v | Source | Status | Checks | Duration |
| --- | --- | --- | --- | --- |
| 1 | approved | verified | 15/15 | 21.6 |
| 2 | note | verified | 13/15 | 21.966667 |

## Director's notes (Muse Spark)

Hook score 4/5. pass

- Working: Opens exactly on brief with finished bowl at 0.00s
- Working: Clear reverse-story logic: hero then raw blank to oiled detail
- Working: Tight 21.6s runtime respects 25s ceiling and keeps momentum

Suggested caption: Finished oak bowl first, then how it's made — blank to lathe to oil.

Review cost: $0.0012 (muse-spark-1.3-contributor).

Full record: `docs/evidence/20261031T083945Z.json`.
