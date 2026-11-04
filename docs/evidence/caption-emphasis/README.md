# Punch-word caption emphasis: live proof

Run on 2026-11-04 at about 10:37 PST. One text-only Muse Spark call, then a real burn of the result onto a local job's cut.

## What was asked

The six caption lines of a local Studio job (`campaigns/cae7cc5be3d14607be87a1f5ef614e4b`, the procedural `workshop.mp4` demo footage) went to Muse Spark with a goal and the language. The lines were rebuilt with `captions.story_cues(plan)` and match the job's `render-1/captions.srt` word for word and to the hundredth of a second. Each word was numbered in the prompt. Nothing was uploaded: the request held only text.

The job ran in local mode, so it had no stored goal. The goal sent was written for this proof: "The oak bowl: a short that shows a raw oak blank becoming a finished bowl on the lathe, for people who like handmade things."

## Model and spend

| | |
| --- | --- |
| Model | `muse-spark-1.3-contributor` (contributor mode, from `.env.local`) |
| Tokens | 591 in, 1,789 out (reasoning included) |
| Spend | $0.0004169 |
| Reserved before the call | $0.00336, against a $0.05 cap |
| Meta response id | `resp_6aeb7b768f7a47047bf0436c` |

Contributor mode lets Meta train on what it receives. These lines are narration for Briefcut's own procedural demo clip, not user footage.

## What was picked, and why

Six lines allow at most two punch words (40%, rounded down), one word a line.

| Line | Word | Spark's reason |
| --- | --- | --- |
| This is the **finished** oak bowl, oiled and ready for the table. | finished (index 3) | Marks completed transformation into finished bowl |
| Here is the **raw** oak blank, still wet and heavy. | raw (index 3) | Marks raw starting point before shaping |

Both picks passed validation unchanged, with no warnings: each names a real line and a word at that place in it. Together they mark the contrast the goal describes, raw to finished. The four other lines stay plain.

## What was burned

The picks became the `edit.captions` patch `{"emphasize": true, "emphasis": [{"cue_key": "565", "word_index": 3, "word": "finished"}, {"cue_key": "8625", "word_index": 3, "word": "raw"}]}`. It passed `edit_spec.validate_edit` merged into the job's edit, and `caption_styles.write_styled_ass` wrote it. FFmpeg's `ass` filter (ffmpeg-full, libass) burned it onto `render-1/01_select_ranges.mp4`. That file is the job's cut before its own captions were added, so the frames show one caption layer only.

The ASS for the boxed style:

```text
This is the {\c&H8CE83C&\fscx115\fscy115}finished{\c&HFFFFFF&\fscx100\fscy100} oak bowl,\Noiled and ready for the table.
```

Every tag used (`\c`, `\fscx`, `\fscy`, `\b1`/`\b0` in the light style, and `\t` in the pop style) is a standard ASS override that libass draws. FFmpeg finished both burns with exit code 0 and nothing on stderr. Both outputs probe as 1080x1920, 18.93 s, yuv420p, with video and audio, the same as the source cut.

- `emphasis-boxed-sheet.jpg`: frames at 0.93 s, 4.18 s and 7.20 s, in the job's own style (boxed). "finished" and "raw" are green and 15% larger. The third line has no pick and is unchanged.
- `emphasis-karaoke-sheet.jpg`: the same picks in the karaoke style, at 1.25 s, 4.45 s and 7.20 s. The yellow spoken-word highlight has moved on to "oak" while the punch word stays green and larger. In the third frame the highlight is on "tells" and there is no punch word.

I looked at both sheets. The text stays inside the safe area, nothing reflows between frames, and the green accent reads apart from the yellow highlight on both the dark wood and the light grain.

## Files

- `emphasis-live.json`: the request (goal, language, numbered lines, full prompt), the model's validated picks with reasons, usage, the ledger entry, the edit patch, and probe results for both burns.
- `emphasis-boxed-sheet.jpg`, `emphasis-karaoke-sheet.jpg`: the contact sheets.
- `proof.py`: the script that made all of it. Rerun it from the repo root with `set -a; . ./.env.local; set +a; export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg; uv run python docs/evidence/caption-emphasis/proof.py JOB_DIR OUT_DIR docs/evidence/caption-emphasis`. A second run with the same inputs is answered from the local cache and costs nothing.

## Limits

- One clip and six lines. The picks were not compared with a human editor's or measured for viewer recall.
- Standard mode (not contributor) reserves room for 16,384 output tokens, about $0.07 for this call, so it needs a cap above $0.05. The call itself would cost about $0.008 at standard prices.
- The model reasoned for 1,789 output tokens to pick two words. A lower `VEDIT_META_REASONING` would likely cost less; that was not measured.
- Translated captions get no emphasis. The picks name English words, and the renderer draws a pick only where the line still has that word at that place.
