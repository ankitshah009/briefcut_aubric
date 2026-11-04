# Scenario matrix, 2026-11-04

This tests whether Briefcut works on footage it was not built around. It ran on real clips, through the paths a judge or an agent uses. It did not change any code. Each run has a JSON record in this folder. Live renders have contact sheets, and each one was looked at.

- **A. Live Meta over MCP.** The exact `.mcp.json` command (`uv run --no-dev --extra mcp --extra sam vedit mcp`), an isolated `VEDIT_WORKSPACE`, and a raw JSON-RPC client. Three public sample clips, one multi-clip project, and one run on `examples/demo/workshop.mp4`. Meta mode was `contributor`, as set in `.env.local`. These are public clips.
- **B. Local only, Meta stubbed.** Real iPhone clips and photos, run through the in-process `CampaignManager`. Every socket connect was blocked, so nothing left the machine. Only numbers are kept here: no file names, frames or caption text.
- **C. Fresh-clone judge path.** `git clone --no-local`, then an rsync of the working tree, then the README quickstart followed literally. The first project ran through the Studio HTTP API.

The code was changing while these ran. Other agents were editing the tree, and each record carries `code.working_tree_diff_sha256`. Times are PST.

## Spend

| Run | Meta calls | Spent (from Meta's reported usage) | Reserved |
| --- | --- | --- | --- |
| A1 | Spark story, SAM tracking (300 frames), Spark find, 2 SAM verify-find | $0.0814 | $0.0870 |
| A2 | Spark story | $0.0005 | $0.0033 |
| A3 | Spark story, Spark find, SAM verify-find | $0.0106 | $0.0166 |
| A4 | Spark story (v2 and v3 made no call) | $0.0010 | $0.0033 |
| A5 | Spark story, Voice Transcribe 40 s, 2 Spark reviews | $0.0095 | $0.0124 |
| C | Spark story, Voice Transcribe 40 s | $0.0039 | $0.0054 |
| **Total** | 16 calls | **$0.1069** | $0.1282 |

The budget was $0.60. These are Briefcut's estimates from token and frame counts, not Meta's bill. Run B made no Meta calls.

## Results

The receipt's probe matched my own ffprobe on duration, width, height, audio and color transfer for every render, except B3 (see bug 9).

### A. Live Meta, MCP

| Run | Input | Ask | Checks | Delivered (own ffprobe) | Plan / render / wall | Verdict on the frames |
| --- | --- | --- | --- | --- | --- | --- |
| A1 | wanrong-walk 1920x1080, 15.6 s, silent track, film boxed in black | 9:16, 10 s, SAM subject | 6 of 6 | 1080x1920, 10.0 s, h264, bt709, audio | 23 s / 28 s / 95 s across sessions | Subject followed, but 79% of the frame is black (bug 2) |
| A1 find | same | "a woman wearing an ornate headdress..." | at 11:22, flagged 0 of 45 frames; at 11:39, verified 19 of 45 | n/a | 29 s, then 11 s | False negative at 11:22; fixed by a later tree change |
| A1 revise auto | same | auto | "No automatic fixes to apply." | n/a | n/a | Correct: nothing failed |
| A2 | flower 960x540, 5 s, silent track | 16:9, 4 s | 5 of 5 | 1920x1080, 4.0 s | 25 s / 3 s / 29 s | Good. Bloom in order, fade visible |
| A3 | whitehouse-walk 720x720, 4.8 s, no audio stream | 9:16, 4 s | 5 of 5 | 1080x1920, 4.0 s, no audio stream | 20 s / 2.5 s / 24 s | Good. Both people in frame throughout |
| A3 find | same | "a man in a dark suit and purple tie" | verified, 45 of 45 frames | n/a | 32 s | Verify-find works on a large subject |
| A4 v1 | wanrong + flower + 720x720 JPG | 16:9, 15 s, title card | 5 of 5 | 1920x1080, 13.63 s | 37 s / 6.6 s / 45 s | Flash of the wrong clip at a cut (bug 3) |
| A4 v2 | agent's own cut via `briefcut_render(plan=...)` | 20 s cut | 4 of 5 (duration) | 1920x1080, 20.0 s | 6.5 s | As asked |
| A4 v3 | `briefcut_revise mode=auto` | auto | 5 of 5 | 1920x1080, 15.0 s | 5.5 s | Dropped the photo the brief asked to end on (bug 4) |
| A5 v1 | workshop.mp4, speech | 1:1, 20 s, review on | 11 of 13 (2 Spark opinions failed) | 1080x1080, 17.67 s | 89 s / 56 s / 145 s | Good, except one burned caption reads "the bowl the bowl" (bug 8) |
| A5 v2 | `briefcut_revise mode=director` | director's patches | 13 of 13 | 1080x1080, 17.67 s | 36 s | Push-in and label added; the doubled caption is still there |

### B. Local, Meta stubbed, real phone footage

| Run | Inputs | Ask | Checks | Delivered | Wall |
| --- | --- | --- | --- | --- | --- |
| B1 | HEVC 10-bit HLG, rotated -90, VFR, 39.3 s | 9:16 | 5 of 5 | 1080x1920 SDR bt709, 16.6 s, upright | 28.5 s |
| B2 | HEVC 10-bit HLG, VFR, 2 audio tracks, 35.7 s | 16:9 | 5 of 5 | 1920x1080 SDR, 13.53 s; highlights clipped (bug 1) | 24.9 s |
| B3 | 6 items: rotated HLG, HLG, HEIC, 945x2048 JPG, rotated VFR SDR mp4, 4032x3024 JPG | 9:16 | 5 of 5 | 1080x1920, video 22.433 s, audio 22.500 s | 44.5 s |
| B4 | 576x576 SDR, 169 s | 16:9 | 5 of 5 | 1920x1080, 30.4 s | 23.1 s |
| B5 | one HEIC photo (grid, decoded to 1920x1440) | 1:1, 5 s | 5 of 5 | 1080x1080, 2.4 s | 5.7 s |

Rotation, VFR, HEIC grids, two audio tracks, mixed shapes and a 169 s source all planned and rendered. The stub chose the moments, so B tests geometry and color, not editorial quality.

### C. Fresh clone

The clone synced in 5 s on a warm uv cache. Studio started, and the README's own example brief on `workshop.mp4` completed through the HTTP API: 5 of 5 checks, 1080x1920, 18.93 s, plan 53 s, render 7 s. Captions, title card and story order are correct (`c1-v1.jpg`). The README problems are listed below.

## Bugs, ranked

1. **HLG phone video comes out with clipped highlights.** The tone map is `zscale=t=linear:npl=100 ... tonemap=mobius` at `src/vedit/media.py:50`, and again with `desat=0` at `src/vedit/project_media.py:87`. So single-clip and multi-clip projects also differ. On one B2 frame, 45.3% of Briefcut's pixels sit at Y >= 235, mean Y 183. The same chain reproduces it exactly (45.1%). With `npl=203` it drops to 26.8%. With hable at `npl=203`, 0.0% are clipped and window detail survives. iPhones record HLG by default. Repro: `ffmpeg -ss 4.8 -i <any iPhone HLG .MOV> -vf "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=mobius,zscale=t=bt709:m=bt709:r=tv,format=yuv420p,format=gray,lut=y='if(gte(val\,235)\,255\,0)',signalstats" -frames:v 1 -f null -`, then read YAVG/255.
2. **Black borders are never detected.** wanrong-walk holds a 468x334 picture in a 1920x1080 frame (`cropdetect`: `crop=468:334:708:342`). The 9:16 render follows the subject but keeps the border: the active picture is 726x596 of 1080x1920, so 79% of the frame is black. All 6 checks pass. Repro: A1's request in `a1.json`.
3. **Multi-clip moments cross clip boundaries.** Spark returned "Flower opening" as 15.2 to 20.2 on the combined clock, but the flower clip is 15.633 to 20.633. The moment opens with 0.43 s (13 frames) of archival film (`a4-cut1.jpg`, frames 0.3 to 0.6). Nothing snaps moments to item edges, and the bounds check passes because the combined file counts as one source. Repro: A4.
4. **The auto fix drops the ending the brief asked for.** `src/vedit/lint.py:220-234` shortens or removes moments from the last one backward. In A4 it removed the colonnade photo ("end on the photo of the colonnade"). The title card still says "...to Colonnade", and v3 passes 5 of 5. Repro: A4, `render_segments` then `revise auto`.
5. **The spending cap is not a hard cap for video calls.** `src/vedit/meta.py:922-925` reserves `tokens * price_in + 16384 * price_out`, using `/responses/input_tokens`. That count left out the video: 488 counted vs 4,300 billed (story), 459 vs 4,319 (find). `_usage` (`meta.py:721-740`) never compares the billed cost with the reservation. The output allowance absorbed the gap on these short clips. On long sources it is unmeasured.
6. **The duration check passes cuts far off target.** `src/vedit/lint.py:212` accepts 50% to 115% + 0.5 s. The README's own 25 s example came out at 18.9 s and passed as "duration matches the target".
7. **Receipts say things that did not happen.** On silent tracks no Voice Transcribe call is made, yet the receipt says "Meta heard no speech in these moments" (`src/vedit/campaign.py:2518`). The caption check says "not requested" although captions were requested (`lint.py:291`, called with `captions=None`). B5, a single photo, got three conflicting warnings: no audio, a silent sound track, and Meta heard no speech. B5 also asked for 5 s and silently got a 3 s target, because `campaign.py:1950` clamps it to the photo's length with no warning.
8. **Phrase repeats are burned into captions.** Voice Transcribe wrote "the bowl the bowl" (A5) and "the bowl bowl" (C) for the same synthetic line. `src/vedit/captions.py:381` collapses only a repeated single word, so A5 v1 and v2 both burned the doubled phrase. Spark's "Captions read easily" check passed both versions.
9. **Audio runs past video in one multi-clip render.** In B3 the audio stream is 22.500 s and the video 22.433 s. The receipt's proof reports 22.433, but the container is 22.5 s.
10. **MCP hides unexpected errors.** During another agent's edit, `meta.py:155` raised `NameError: name '_base_url' is not defined`. The agent saw only "Error executing tool briefcut_status" (`a2-first-attempt-broken-tree.json`).
11. **Fixed during this run: verify-find missed small subjects.** At 11:22 the SAM window was 640x360 and the woman was flagged as never seen (0 of 45 frames). `src/vedit/verify_find.py` changed at 11:36 (`WINDOW_HEIGHT = 540`). At 11:39 the same search verified her in 19 of 45 frames.

## README problems (judge path)

1. The doctor step comes before the key step, but the README says every `[required]` row must pass. The `meta` row is required and cannot pass yet. `vedit doctor` never reads `.env.local`, so it still fails after the key is pasted unless the shell loads the file.
2. `cp .env.example .env.local` with the key in the shell breaks Meta. `scripts/studio.sh` sources the file with `set -a`, so its empty `MODEL_API_KEY=` and `VEDIT_META_MODE=standard` overwrite the shell. Studio then showed `configured: false, mode: standard`. `.env.example` says "A variable already set in your shell wins over this file". That is true for the Python loader, not for `studio.sh`.
3. `scripts/install.sh` also runs `uv tool install --force --reinstall` for a global `briefcut`. The README says only that it installs uv and FFmpeg.
4. "Make a first video" covers the browser only. The HTTP API a headless judge needs is in `docs/CAMPAIGN.md`, which the quickstart does not point to. `docs/CAMPAIGN.md:263` still lists `provider="local"` and `caption_provider="local"` as the `briefcut_create_story` defaults, and the server refuses both.
5. The README holds 10 `<!-- VERIFY -->` notes and a "Private mode (not yet confirmed) ... Not available in this build." section.
6. The README's example asks for a 25-second story and gets 18.9 s, which passes (bug 6).

What worked as written: `uv sync`; `vedit meta check` after the `set -a` line (it exits 1 without a key and does not read `.env.local` itself); Studio on `127.0.0.1`; all 27 relative links in the README resolve.

## Not covered

- No browser session. Studio's page states were not seen, only its HTTP routes.
- No live run on speech from a real person. The only speech was the synthetic narration in `workshop.mp4`.
- No live SAM cutout, cover, translation, shorts or script to video.
- No live long source (over 10 minutes), so bug 5 has no measured overrun.
- In run B the moments came from a stub, so B says nothing about Spark's choices on phone footage.

## Files

`a1.json` to `a5.json` hold the request, the MCP tool log with timings, the plan, every render's receipts and checks, my own ffprobe, probe agreement, Meta calls with request ids, and the findings. `b-local-phone.json` holds run B's numbers and the HDR measurement. `c-fresh-clone.json` holds the quickstart steps and the first project. The images are contact sheets of the public renders only.
