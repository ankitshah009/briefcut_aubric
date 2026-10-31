# Latest measured run

Recorded 2026-10-31T00:52:36+00:00 by `scripts/demo_run.py`. One run, one machine. Timings are wall-clock and include HTTP polling. Re-run with `uv run python scripts/demo_run.py` to refresh.

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
| Clip | `tracking_demo.mp4` |
| Source | 12.00 s, 1280×720, audio=True |
| Provider | meta |
| Target | 10.0 s, 9:16 |
| Captions | False (none) |
| Effect | None |
| Subject tracking | red square |

## Timings

| Stage | Seconds |
| --- | --- |
| Upload | 0.002 s |
| Time to editable plan | 28.844 s |
| Time to verified render | 24.275 s |
| Total | 53.122 s |

## Plan

Provider `meta`, analysis `none recorded`. 1 segment(s), 10.0 s.

| # | Source range | Label | Reason |
| --- | --- | --- | --- |
| 1 | 1.00–11.00 s | red square in motion | Red square is visible in motion during this range, providing continuous trackable movement needed for centered vertical presentation |

## Verified output

| Item | Server proof | Local probe of the downloaded file |
| --- | --- | --- |
| Duration | 10.0 | 10.0 |
| Size | 1080×1920 | 1080×1920 |
| Audio / video | True / True | True / True |
| Codec | — | h264 @ 30.0 fps |
| Color | bt709 / tv (bt709/bt709) | not reported by `vedit probe`; see report.json |
| Bytes | 392130 | 392130 |

Meta spend reserved: $0.0621 of a $0.50 cap. Local mode makes no model calls.

Warnings from the render: none

Full record: `docs/evidence/20261031T005236Z.json`.
