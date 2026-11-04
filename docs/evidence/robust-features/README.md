# Hero word and verified find, off the oak-bowl clip

Recorded 2026-11-04. Both features were first proven on one procedural clip, an oak bowl on a workbench. This run puts them on footage of people, on a light background, in three shapes, on a silent track, on non-English searches, and on a cut re-trimmed after the word was set.

Only public samples were sent to Meta: `whitehouse-walk.mp4` and `wanrong-walk.mp4` from `~/GitHub/verified-video-search/data/previews/`, and `examples/demo/workshop.mp4`. Speech was heard on this Mac by whisper.cpp (`ggml-base.en`), at no cost.

## Spend and models

| Part | Model | Calls | Cost |
| --- | --- | --- | --- |
| Verified find: search | `muse-spark-1.3-contributor` | 9 | $0.0027 |
| Verified find: check | `sam-3.1` | 6 (50 frames billed each) | $0.0600 |
| Hero word: pick | `muse-spark-1.3-contributor` | 2 | $0.0012 |
| Hero word: matte | `sam-3.1` | 4 (54 to 60 frames) | $0.0456 |
| **Total** | | **21** | **$0.1094** of the $0.40 cap |

Every request id is in `results.json` under `meta_calls`. The second hero pick and its SAM call are real repeats: the prompt changed (it now names the reason's language as "English", not "en"), so the cache missed, and Spark picked "bowl" where it first picked "oak". The final rerun, after the last code change, sent nothing. Contributor mode is on, so Meta may train on what it received: two public clips, 1.8 to 3 s proxies of them, and the demo clip's transcript.

## Hero word

| Scenario | Clip | What happened |
| --- | --- | --- |
| No sound at all | whitehouse (no audio stream) | Refused before any call: "A hero word needs speech, and no words were heard in this cut. Nothing was sent to Muse Spark." |
| A silent track that whisper still "hears" | wanrong (peak -91 dB) | whisper.cpp heard "You" in 15 s of digital silence. Refused before any call: only filler words. |
| An invented "Thank you." on that silence | wanrong, transcript simulated by the proof | Refused before SAM: "the clip is silent while it should be said (peak -91 dB)". New check. |
| Two people, light colonnade, subject "the two people" | whitehouse 1:1, delivered 16:9, 9:16, 1:1 | Centre would hide 68 to 94 % of the word, so it moved to `top` (5 to 7 % hidden). White read at 1.9 to 2.2:1 on the white walls, so the word became near-black (5.7 to 7.5:1). All three receipts pass. |
| Two people, subject "the man in the dark suit" | whitehouse, three shapes | 9:16: centre, 46 % hidden, white on the woman's dark suit (10.0:1). 16:9 and 1:1: at centre the word sat on a dark suit and a white wall, and no colour reached 3:1 (best 2.3:1). It moved to `top` (clear of the man in 16:9, 6 % behind him in 1:1) and turned black (7.3:1, 5.7:1). All three receipts pass. Before the side could follow contrast, the 16:9 and 1:1 receipts failed at 2.3:1. |
| Re-trimmed after the word was set | workshop, cut moved from 0-5 s to 1-5 s | Spark picked "bowl" on the original cut (1.53-3.53 s). After the trim, "bowl" is said at 0.59 s but the window still opened at 1.53 s. Without `reanchor`: refused before SAM: "The cut changed after the hero word was set: “bowl” is now said at 0.59 s, but its window opens at 1.53 s. Pick the hero word again." With `reanchor=True`: moved to 0.53-2.53 s, SAM masked 60 of 60 frames, side `top` (15 % hidden), white kept on the dark planks (12.6:1). Receipt passes. The first live run, with Spark's "oak", behaved the same way. |

Local checks, no spend (`cjk-local.jpg`): 东京, 富士山 and 서울 render through libass's font fallback, with the subject in front. A transcript that splits 东京 into 东 and 京 now matches.

Contact sheets (before, inside, after the window), all looked at:

- `hero-people-16x9.jpg`, `hero-people-9x16.jpg`, `hero-people-1x1.jpg`: "WALK" in near-black above both heads; the man's head passes in front of the letters.
- `hero-man-16x9.jpg`, `hero-man-9x16.jpg`, `hero-man-1x1.jpg`: 16:9 and 1:1, black "WALK" above the heads, small in 16:9 because the man's head leaves little room; 9:16, white "WA" over the woman with the man in front of "LK".
- `hero-retrim-16x9.jpg`: "BOWL" in white on the dark planks, the rim in front of the lower letters, at 0.53 to 2.53 s of the re-trimmed cut.

Every render was probed: 4.83 s (4.0 s for the re-trim), 1920x1080, 1080x1920 or 1080x1080, video present, audio present only where the source has it, bt709 tv. The word "WALK" on whitehouse was supplied by the proof script because the clip has no speech. The product refuses that clip, as the first row shows.

## Verified find

Muse Spark searched each clip. Its new `subject` field named what SAM looked for; the fixed rule is the fallback.

| Query | Clip | Spark's subject | Rule alone | Result |
| --- | --- | --- | --- | --- |
| the woman walking | whitehouse | woman | woman | 1 moment, 44 of 44 frames, verified |
| someone waving | whitehouse | person | person (was nothing) | Spark found no moment; nobody waves. Nothing to check |
| the purple tie | whitehouse | purple tie | purple tie | 44 of 44 frames, mask 1 % of the picture, verified |
| la mujer caminando | whitehouse | woman | not sent (not English) | verified, same SAM answer as "the woman walking", so no second charge |
| 走路的男人 | whitehouse | man | not sent (not English) | 44 of 44 frames, verified |
| the men in dark suits | whitehouse | men in dark suits | men | 45 of 45, verified. SAM's main object is the nearest man, so a plural checks that at least one is on screen |
| where I mention pricing | whitehouse | none | spoken | SAM skipped, as it should |
| a dog | whitehouse | dog | dog | Spark found nothing; nothing to check, nothing spent on SAM |
| the person walking | wanrong (small picture in a black frame) | person | person | 2 moments, 45 of 45 each, masks 1.2 to 1.4 % of the frame, verified |

`find-contact-sheet.jpg` shows each checked frame with SAM's mask outlined. The woman, the tie, the man and both small figures on wanrong are outlined correctly. The label for the Chinese query shows boxes because the label font lacks those glyphs; the search itself ran in Chinese.

Not run live, covered by tests on recorded SAM output: a SAM miss is flagged, kept and ranked last (default), and the budget cap refuses before any frame is sent.

## Files

- `results.json`: every scenario, the checked edits, matte reports, contrast by colour and side, receipts, render probes, Spark moments and subjects, SAM evidence, the call ledgers.
- `hero-*.jpg`, `find-contact-sheet.jpg`, `cjk-local.jpg`: the sheets above.
- `proof.py`: the script. Rerun with the key loaded only for that command:

```sh
set -a; . ./.env.local; set +a
VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg uv run python docs/evidence/robust-features/proof.py WORK_DIR
```

## Limits

- No public clip has a person speaking on camera, so the full chain (speech, Spark's pick, SAM on a person) ran only on the demo clip. On people, the word was supplied.
- SAM's matte keeps one identity (`meta._selected_object`). With two people and subject "the man", the other person stands behind the word, not in front of it.
- The colour is a flat fill with a soft shadow. On a background that is both light and dark, no flat colour may reach 3:1; the receipt then fails and says so. An outline would fix it, and the text layer has none (`\bord0` in `overlays.render_hero_ass`).
- CJK glyphs fill more of the em than Latin capitals. The layout assumes capitals, so a CJK word set `top` sits close to the frame's top edge.
