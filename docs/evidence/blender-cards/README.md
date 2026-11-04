# 3D motion card: live run, 2026-11-04

Muse Spark designed an intro card, Muse Image painted its backdrop plate, and Briefcut's fixed Blender template rendered it. The compiler then laid the card over the first 3 seconds of an existing 1080x1920 render. The timing did not change.

## What ran

1. **Muse Spark** (`muse-spark-1.3-contributor`) read the goal "Show how the oak bowl is made, start to finish" and the cut's six caption lines. It returned bounded JSON:
   - kind `intro_title`
   - text "Oak Bowl Start To Finish"
   - subtitle "From Wet Blank To Table Ready"
   - style `warm_wood`, palette `oak`, camera `dolly_in`, 3.0 s

   `edit_spec.validate_motion_card` accepted it without changes. Spark wrote no code. Its words were set as a Blender text body and nothing else.
2. **Muse Image** (`muse-image-1.0`) painted the plate from Spark's one-sentence description: "Softly blurred close-up of oak shavings and a bowl blank resting on a workshop bench." The card opens on that plate, then the plate dissolves into the footage while the title stays. See `muse-image-backdrop.jpg`.
3. **Blender 5.2.3 LTS** ran `src/vedit/assets/blender/motion_card.py` headless (`-b --factory-startup`) with Eevee (`BLENDER_EEVEE`) at 1080x1920, 30 fps, and 16 samples. It wrote 90 RGBA frames. FFmpeg added the alpha fades and encoded them to ProRes 4444 with alpha (`yuva444p10le`, 48.7 MB).
4. **The compiler's `motion_card` op** fused the card into the export filter chain. It loads the card with `movie`, offsets it with `setpts` to the window, and lays it on with `overlay` using `eof_action=pass` and `enable=between(t,0,3)`. There was no extra encode and no timing change.
5. A second compile of the same plan found the card in the cache (`~/.vedit/cards/3f59d102579ca67e0ed0/`) and scheduled no Blender step.

## Proof

| | Value |
| --- | --- |
| Input render | 18.933 s, 1080x1920, audio, video |
| Output (`report.json`) | 18.933 s, 1080x1920, 30 fps, has_audio true, has_video true, BT.709 |
| Card | 90 frames, 3.000 s, ProRes 4444 with alpha |
| Blender, whole process | 24.17 s |
| Blender, render loop | 23.49 s for 90 frames (0.26 s a frame) |
| Encode PNG to ProRes | 0.4 s |
| Second render of the same plan | 0 s (cache hit, no Blender step) |

The machine is an Apple M5 Max with 18 cores. Other test suites were running at the time, with a load average of about 36. Earlier iterations of the same card under a load of 150 to 300 took 36 to 60 s.

## Spend

| Call | Model | Cost |
| --- | --- | --- |
| Design the card | muse-spark-1.3-contributor (700 in, 1,712 out tokens) | $0.0004 |
| Backdrop plate | muse-image-1.0 | $0.0100 |
| **Total** | | **$0.0104** of the $0.05 allowed |

Only the goal and the cut's transcript text were sent. No media was uploaded. The clip is Briefcut's procedural demo footage.

## Files

- `contact-sheet.jpg`: frames 9, 33, 57 and 84 of the composited output. They show the fade in on the plate, the plate with the title, the hand-over to the footage, and the title over the footage just before it fades.
- `detail-frame-66.jpg`: a full-resolution crop showing the bevel, the extrusion, the grain, the drop shadow and the scrim.
- `muse-image-backdrop.jpg`: the Muse Image plate, downscaled.
- `spec.json`: Spark's proposal, the validated card, the render spec the template read, the render record, the Meta calls, and the composite's probe.
- `blender-log.txt`: the Blender version line, the first and last frames saved, and the timings.

## Reproduce

```sh
export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
uv run python -m vedit.blender_cards check
echo '{"kind":"intro_title","text":"Oak Bowl Start To Finish","subtitle":"From Wet Blank To Table Ready","style":"warm_wood","palette":"oak","camera":"dolly_in","duration":3.0}' > card.json
uv run python -m vedit.blender_cards render --card card.json --width 1080 --height 1920 --fps 30 --backdrop plate.png
```

In a brief, `{"op": "motion_card", "card": {...}}` schedules the Blender step itself when the card is not cached yet.

## Limits

- The look was judged by eye on this one clip and on a low-resolution gallery of all five styles. No viewer study was done.
- Wood letters over wood footage depend on the scrim for contrast. Spark picked the material that matches the subject, so this case is the hard one.
- Line breaks follow a typesetting rule, not meaning. "Oak Bowl / Start / To Finish" reads well. Other titles may break less naturally.
- The fonts are macOS system fonts, with DejaVu as the Linux fallback. A glyph a font lacks is not drawn. Emoji and symbols are refused at validation.
- Blender's text objects lay letters out left to right and never join them. Rendered live in Blender 5.2, "مرحبا بالعالم" came out as isolated letters in reverse reading order ([rtl-drawn-backwards.jpg](rtl-drawn-backwards.jpg)). So a card with Arabic or Hebrew in its title or subtitle is now refused, both when Spark proposes it and when it is rendered. Words with combining marks (Hindi vowel signs, Thai) are already refused by the edit's validation.
- A cached 3 s card is about 49 MB, in `~/.vedit/cards`. After each fresh render the cache drops its least recently used cards until it fits 2 GB (`VEDIT_CARD_CACHE_MB` changes the cap). A card used in the last 10 minutes is never dropped, and reading a cached card's path for a compile counts as a use.
- Two renders of the same card take turns on a lock in its folder, so Blender runs once and the second render finds the card cached. Each render draws and encodes in its own work folder, and the card appears by an atomic rename only after it has been probed. `tests/test_review_features.py` runs four renders of one card at once to show this.
- The cache key does not include the font files' bytes or the Blender version. A font replaced at the same path, or a Blender upgrade, reuses the old card until the cache drops it.
- Blender is optional. Without it the op leaves the card out with a warning, and `blender_available()` lets Studio hide the control.
