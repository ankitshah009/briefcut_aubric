# Latest measured run

Recorded 2026-10-31T01:20:14+00:00 by `scripts/demo_run.py`. One run, one machine. Timings are wall-clock and include HTTP polling. Re-run with `uv run python scripts/demo_run.py` to refresh.

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
| Target | 20 s, 9:16 |
| Captions | True (meta) |
| Effect | {'preset': 'push', 'intensity': 0.55, 'start': 0, 'duration': 3} |
| Subject tracking | off |

## Timings

| Stage | Seconds |
| --- | --- |
| Upload | 0.004 s |
| Time to editable plan | 32.284 s |
| Time to verified render | 79.255 s |
| Total | 111.542 s |

## Plan

Provider `meta`, analysis `meta`. 5 segment(s), 18.0 s.

| # | Source range | Label | Reason |
| --- | --- | --- | --- |
| 1 | 0.00–2.70 s | Finished bowl reveal | Visible tan card reads '1. Finished bowl', opening the story with the completed piece as brief requires. Rests on speech [0.0–2.7] 'This is the finished oak bowl, oiled and'. |
| 2 | 8.00–11.20 s | Raw oak blank | Visible dark card reads '2. Raw oak', showing the starting material. Rests on speech [8.0–11.2] 'Here is the raw oak blank, still wet and'. |
| 3 | 16.00–20.00 s | Turning on lathe | Visible blue card reads '3. Turning', showing shaping step. Rests on speech [16.0–18.8] 'On the lathe the shape appears in long,' and [18.8–20.0] 'steady cuts.'. |
| 4 | 24.00–29.00 s | Sanding smooth | Visible brown card reads '4. Sanding', showing smoothing step. Rests on speech [24.0–26.4] 'Sanding goes from coarse to fine, with' and [26.4–29.0] 'the lathe turning slowly.'. |
| 5 | 32.00–35.10 s | Oil finish | Visible light card reads '5. Oil finish', showing final coating step. Rests on speech [32.0–35.1] 'A coat of food safe oil brings out the'. |

## Verified output

| Item | Server proof | Local probe of the downloaded file |
| --- | --- | --- |
| Duration | 18.0 | 18.0 |
| Size | 1080×1920 | 1080×1920 |
| Audio / video | True / True | True / True |
| Codec | — | h264 @ 30.0 fps |
| Color | bt709 / tv (bt709/bt709) | not reported by `vedit probe`; see report.json |
| Bytes | 1212356 | 1212356 |

Meta spend reserved: $0.0176 of a $0.50 cap. Local mode makes no model calls.

Warnings from the render: none

## Director's notes (Muse Spark)

Hook score 2/5. Structure is correct but cut reads as title cards over flat color, not a bowl story. No finished bowl or process is actually seen, and the cropped opening kills the hook. Replace cards with real footage and fix titles to serve the 20-second reveal-then-make goal.

- Working: Follows approved story order: finished reveal then raw, turning, sanding, oil finish.
- Working: Even chapter pacing across 18.0s suits vertical short attention span.
- Working: Numbered lower-thirds plus subtitles keep steps clear even without sound.
- Working: Closes on oil finish, giving a complete making arc.
- Fix: 0.0-3.0s opening hook shows flat tan card and cropped title '. Finished bow' with cut-off line 'oiled and' - replace with real finished-bowl hero close-up and fix to '1. Finished bowl'. (at 1.0s)
- Fix: 3.0-6.0s Raw oak section is dark green card with no wood visible - lay in actual raw blank footage per outline to show wet/heavy starting point. (at 4.5s)
- Fix: 6.5-9.5s Turning section is blue card with no lathe action and stuttered subtitle 'the the shape' - add real turning cuts and clean audio/caption. (at 7.5s)
- Fix: 10.0-14.0s Sanding section is brown card with no hands or dust visible and duplicated 'lathe lathe' - add real sanding detail and tighten edit. (at 11.5s)
- Fix: 15.0-18.0s Oil finish closer is grey card with no application visible - add real oiling wipe to pay off transformation and hold end frame for story close. (at 16.0s)

Suggested caption: Finished bowl first, then how it happens: raw oak to turning to sanding to oil.

Review cost: $0.0009 (muse-spark-1.3-contributor).

Full record: `docs/evidence/20261031T012014Z.json`.
