# Latest measured run

Recorded 2026-10-31T00:16:26+00:00 by `scripts/demo_run.py`. One run, one machine. Timings are wall-clock and include HTTP polling. Re-run with `uv run python scripts/demo_run.py` to refresh.

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
| Provider | local |
| Target | 20 s, 9:16 |
| Captions | True (local) |
| Effect | {'preset': 'float', 'intensity': 0.55, 'start': 0, 'duration': 3} |
| Subject tracking | off |

## Timings

| Stage | Seconds |
| --- | --- |
| Upload | 0.009 s |
| Time to editable plan | 1.524 s |
| Time to verified render | 18.227 s |
| Total | 19.760 s |

## Plan

Provider `local`, analysis `local-analysis`. A local plan is deterministic signal analysis, not model understanding. 6 segment(s), 19.92 s.

| # | Source range | Label | Reason |
| --- | --- | --- | --- |
| 1 | 0.38–3.87 s | Opening hook: bowl, finished | Speech 0.5–3.7s (100% of the moment); mentions "bowl", "finished", "oak" from your brief; first spoken moment. |
| 2 | 8.41–11.53 s | Spoken moment: oak | Speech 8.6–11.4s (100% of the moment); mentions "oak" from your brief; follows a scene change at 8.0s. |
| 3 | 16.38–19.47 s | Spoken moment | Speech 16.5–19.3s (100% of the moment); follows a scene change at 16.0s. |
| 4 | 24.38–28.36 s | Spoken moment | Speech 24.5–28.2s (100% of the moment); follows a scene change at 24.0s. |
| 5 | 32.38–35.25 s | Spoken moment | Speech 32.5–35.1s (100% of the moment); follows a scene change at 32.0s. |
| 6 | 35.98–39.34 s | Closing moment: bowl | Speech 36.1–39.2s (100% of the moment); mentions "bowl" from your brief; last spoken moment. |

## Verified output

| Item | Server proof | Local probe of the downloaded file |
| --- | --- | --- |
| Duration | 20.0 | 20.0 |
| Size | 1080×1920 | 1080×1920 |
| Audio / video | True / True | True / True |
| Codec | — | h264 @ 30.0 fps |
| Color | bt709 / tv (bt709/bt709) | not reported by `vedit probe`; see report.json |
| Bytes | 1265387 | 1265387 |

Meta spend reserved: $0.0000 of a $0.25 cap. Local mode makes no model calls.

Warnings from the render: none

Full record: `docs/evidence/20261031T001626Z.json`.
