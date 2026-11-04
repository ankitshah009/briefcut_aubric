# Live integration proof: Studio routes against live Meta

Date: 2026-11-04, 12:13 to 12:29 PST. Briefcut 1.2.0. Meta mode: contributor.

Every step below went through Studio's own HTTP routes. These are the same routes the page calls: `/api/upload`, `POST /api/campaigns`, `GET /api/campaigns/{id}`, `/render`, `/revise`, `/find`, `/find-thumbnail`, `/hero-word`, `/motion-card`, `/meta-calls`, `/assets`, `/api/settings` and `/api/capabilities`. No module was called directly. Every delivered file was downloaded through `/artifact?name=video` and probed with our own ffprobe (`ffmpeg-full`).

## Setup

- Studio ran on port 8793 with the key loaded from `.env.local`. The key was never printed. `grep` finds no key material in this folder.
- `VEDIT_WORKSPACE` and `VEDIT_HOME` pointed at a scratch folder. So the settings change in run 6 never touched `~/.vedit/config.json`. `cards` and `models` were symlinked to `~/.vedit`, so the Blender cache is the real one.
- Muse Glimmer 30B was already serving on 127.0.0.1:8091 (llama.cpp b11669).
- Footage: `examples/demo/workshop.mp4` (40.0 s, 1280x720, speech) and `verified-video-search/data/previews/wanrong-walk.mp4` (15 s, 1920x1080, silent).

## Results

| Run | What | Wall time | Live spend | Result |
|---|---|---|---|---|
| 1 | workshop, language auto, captions, review, director wonder, $0.60, 9:16. Plan, then render v1 | plan 50.3 s, render 54.4 s | $0.1035 | PASS |
| 2 | `captions.emphasize=true` through the render route, v2 | 28.1 s | $0.0005 | PASS (Wonder review refused, bug 1) |
| 3 | `POST hero-word`, render with the returned edit, v3 | 18.6 s + 33.2 s | $0.0126 | FAIL with the default subject (bug 2) |
| 3b | `POST hero-word` with subject "the wooden bowl", v4 | 28.9 s + 44.7 s | $0.0112 | PASS |
| 4 | `POST motion-card` (intro_title, backdrop), render v5 | 20.5 s + 54.4 s | $0.0104 | PASS (bug 4 on captions) |
| 5a | find "the wooden bowl" on workshop, verify on | 42.9 s | $0.0116 | PASS with a wrong badge (bug 3) |
| 5b | wanrong-walk, own project, find "the woman walking" | 22.1 s + 25.5 s | $0.0113 | PASS |
| 6 | private_model glimmer, revise note "Hold the first shot longer", v6 | 70.4 s | $0.0000 | PASS |
| 7 | captions.language "es", render v7 | 18.1 s | $0.0002 | PASS (bug 5) |
| 7b | extra: private mode, captions.language "fr", v8 | 36.2 s | $0.0000 | PASS |
| 8 | `GET /meta-calls` for both projects against the ledgers | n/a | $0 | PASS |

Total live Meta spend: **$0.1612** of the $1.20 allowance. Workshop: $0.149892 over 13 API calls and 2 local calls. Wanrong: $0.011307 over 3 API calls.

### What each run showed

1. The transcript came from `meta-voice-transcribe` (`plan.analysis.transcribed_by`). Muse Wonder reviewed the cut: 11 checks with `by: muse-wonder` and `basis: inferred`. Studio labels these "Model opinion" and "Muse Wonder" (`studio.js:588-589`). Our probe matches `result.proof`: 18.433 s, 1080x1920, 30 fps, H.264 and AAC, bt709, audio and video present. The log has 3 rows: transcribe $0.0020, story $0.0016, Wonder review $0.0999.
2. `result.emphasis` names "bowl," and "raw". Both show green and scaled in the burned captions. See `run2-emphasis-bowl-1.5s.jpg` and `run2-emphasis-raw-4.2s.jpg`.
3. With no subject, the route picks "the speaker" (`campaign.py:2265`). This clip shows no person. SAM was billed $0.012 and found nothing. The receipt failed: "auto: hero word sits behind the subject: no subject matte was made". With "the wooden bowl", the receipt passed: "SAM masked 100% of 52 frames". `run3-hero-word-17.5s.jpg` shows BOWL behind the bowl's rim.
4. Blender 5.2.3 ran and wrote a new cache entry `f9114ebe7fcc36416bfe`: 90 frames, ProRes 4444 with alpha, about 38 s. v6 to v8 reused it. `run4-motion-card-first-3s.jpg` shows 3D wood letters on the Muse Image backdrop, with a dolly in.
5. SAM verified both finds. The thumbnail route returns a JPEG, and `name=../meta-usage.json` gets a 404. The wanrong source has a small picture inside a black frame, so its thumbnail does too.
6. The patch row reads `runs_on: local`, `meta-models/Muse-Glimmer-30B`, $0, 57 s. API spend before and after this step was the same, $0.1497286. Glimmer chose `set_speed 0.75` on moment 1, and the cut grew from 18.43 s to 19.57 s. `private_model` was set back to "".
7. The burned captions are Spanish (`run7-spanish-captions.jpg`). One Muse Spark row: "Translate 6 caption lines to Spanish", $0.00016. In 7b, French was translated on Glimmer, local, at $0.
8. For both projects the route's calls, spent, held, local count and remaining match a sum over `meta-usage.json`. Every ledger id is also in `meta-calls.jsonl`. See `run8-ledger-vs-route.json`.

## Bugs found (ranked)

1. **A finished call stays reserved at its maximum, so Muse Wonder can review a $0.60 project only once.** `src/vedit/meta.py:850-851` counts `max(reserved_usd, cost_usd)` for every call, completed ones included. The comment at `meta.py:839` says only ambiguous charges should stay held. The Wonder review cost $0.0999 but holds $0.3661. Every later render then prints "Director's notes unavailable: A Muse Wonder review can cost up to $0.3277 ... $0.3762 of the $0.6000 campaign budget is already reserved. Raise the campaign budget to at least $0.71". That happened on v2 to v8. `/meta-calls` shows `held_usd 0.4375` with `running 0`. Repro: create with director wonder, review true, budget 0.60. Render twice. Read `result.review.error` on v2. The same message says "Standard mode only" while the project runs in contributor mode (`meta.py:1769`).
2. **With no subject, the hero word asks SAM for "the speaker".** `src/vedit/campaign.py:2265` falls back to `"the speaker"`. Studio sends no subject when the project has none (`inspector.js:1064-1071`). On footage with no person, SAM is billed and the word is dropped. Repro: `POST /api/campaigns/{id}/hero-word` with `{}` on workshop, then render with `edit.hero_word`. Warning: "SAM could not find “the speaker” during the hero word, so it is left out."
3. **Find moves SAM's verdict and thumbnail onto a moment SAM never saw.** `src/vedit/director.py:382` treats two moments as twins when they share half of the shorter one. Spark returned one moment for the whole clip (0 to 40.021 s). It merged into the transcript hit at 0.383 to 3.58 s. Then `director.py:392` copied SAM's verdict and thumbnail onto that hit. SAM had looked at 18.511 to 21.511 s (`verify-windows/window-...-18511-21511.mp4`). The card shows "verified" on 0.38 to 3.58 s with a lathe frame from 18.5 s. Repro: find "the wooden bowl" on workshop. Compare `moments[0]` with `verification.spend[0].window`.
4. **The motion card covers the captions, and the receipt still passes.** `src/vedit/render_plan.py:323` places `motion_card` after `captions`. So the card's backdrop hides the speech captions from about 0.4 s to 1.6 s. `run4-motion-card-first-3s.jpg` shows this while the first line is spoken. "auto: captions cover the speech" still reports 100%.
5. **A long translated line leaves a short orphan cue.** v7, Spanish: "Este es el cuenco de roble / terminado, aceitado y listo para" runs from 0.03 to 3.95 s. Then "la mesa." shows alone for 0.56 s. In French, "tourne lentement." shows alone for 1.0 s. See `render-7/captions.ass` and `run7-v7-es-contact.jpg` at 3.26 s. Translation also drops the emphasis without a warning. The card text and the hero word stay in English.

Smaller notes: the green accent also colors the comma in "bowl,". The `consent` field in the create payload is ignored without an error.

## Files

- `runN-*.json`: one per render, with receipts (`checks`), review summary, patches, our probe next to `result.proof`, every Meta call row, route totals and the ledger sum.
- `runN-*-contact.jpg`: 12 frames per render. All were looked at.
- `run3-hero-word-response-*.json`, `run4-motion-card-response.json`, `run4-blender-card-result.json`, `run5a-find-wooden-bowl.json`, `run5b-find-woman-walking.json`, `run6-private-note.json`, `run8-ledger-vs-route.json`.
- Frames: `run2-emphasis-*.jpg`, `run3-hero-word-17.5s.jpg`, `run4-motion-card-first-3s.jpg`, `run5-find-thumbnail-*.jpg`, `run7-spanish-captions.jpg`, `run7b-french-private-6s.jpg`.

Projects: workshop `4895f70caaf44029aed8e62c865e241b`, wanrong `d58a2cc491e0499ba880f4fde7dc7d9f`.
