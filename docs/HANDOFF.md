# Handoff

Written 2026-10-31 for the agent taking over Briefcut. Read this, then `AGENTS.md` and `docs/EDITOR_ARCHITECTURE.md`, before changing anything.

## The goal

Briefcut is the user's entry for the Meta Global AI Developer Hackathon. Submissions close **November 4, 2026, at 9:00 PM ET**. The user's words: "you need to build a real product here", at "parity or better than CapCut". It has to work on anyone's footage, not just the demo clip.

`docs/CAPCUT_PARITY.md` is the honest scoreboard. Keep it honest: a row changes only when the feature has run on real media.

## State of the tree

- Branch `briefcut-demo-polish`. One commit (`b3c2a64`) on top of `main` (`56989fe`), plus a large uncommitted working tree. All of it is the work.
- **Do not commit or push unless the user asks.** The user did not ask, and an earlier commit to `main` had to be moved.
- `.env.local` holds a working Meta key. Load it with `set -a; . ./.env.local; set +a`. Reports in this project that say "no Meta key on this Mac" are wrong; those runs simply had not loaded it.
- The Whisper model is at `~/.vedit/models/ggml-base.en.bin`.

## Rules

- Python through `uv` only: `uv run`, `uv add`. Never bare `pip` or `python`.
- `export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg`.
- Tests: `uv run pytest -n auto`. `tests/conftest.py` sizes workers to the cores that are free, so `-n auto` is always right. Add `-o addopts= -rf` to see totals and failure names; the default `-q` hides them. Do not run several full suites at once. Heavy overlap pushed the load past 100 and produced false failures.
- Never invent FFmpeg flags. Confirm with `ffmpeg -h filter=<name>`. New ops go in `OPS` in `src/vedit/compile.py` with a test.
- A render is proven only by probing the output file and reading `report.json`.
- One clock: all timing goes through `src/vedit/timeline.py`. Nothing after `select_ranges` may change timing.
- `plan["edit"]` is bounded data validated by `src/vedit/edit_spec.py`. Models propose; they never execute.
- **Meta spending:** about $2.21 spent so far. Your ceiling is another $3.00. Use `examples/demo/` clips or the public samples in `~/GitHub/verified-video-search/data/previews/`.
- **Never send these to any API:** anything in `~/Downloads`, or the user's clips in `~/GitHub/gemini_editor/footage/hackathon`. Contributor mode lets Meta train on what it receives. Both folders are fine for local testing.
- **SAM license:** `meta-sam-parser` is under Meta's SAM License, which bars reverse engineering. Use only its public decoder. Do not read into its private modules or copy its codec. The test `test_only_the_public_decoder_is_used` guards this.
- Writing: plain short sentences, no em dashes, no claim beyond the evidence.

## What exists

| Area | State | Main files |
| --- | --- | --- |
| One clock, edit spec, render order | Done | `timeline.py`, `edit_spec.py`, `render_plan.py` |
| Real footage | 4 of 4 real clips pass 5/5; 132 of 132 tests in the matrix | `tests/test_real_footage.py`, `media.py` |
| Multi-clip, photos, assets, filmstrip, waveform | Done; 4 GB and 2 hours | `project_media.py` |
| Captions: word timing, 5 styles, edits, fillers, translation | Done | `captions.py`, `caption_styles.py` |
| Looks, zoom, stabilize, voice, music, text, logo, export | Done with limits below | `looks.py`, `motion.py`, `overlays.py`, `sound.py`, `beats.py` |
| SAM cutout, generated backgrounds | Done | `matte.py`, `meta.py` |
| Director: note edits, auto-refine, find, shorts, script to video | Done | `director.py`, `lint.py` |
| Studio editor | Done | `static/timeline.js`, `preview.js`, `inspector.js`, `mediabin.js` |
| First run, settings, projects, install, DMG | Done with limits below | `onboarding.py`, `settings.py`, `projects.py`, `scripts/install.sh` |

## Finish list, in order

### 1. Get a clean full suite

Baseline at handoff: **1,528 passed, 1 failed, 4 skipped** in 5m 42s with 14 workers at load about 40. That is the first complete run on the finished tree.

- The failure is real and repeatable: `tests/test_plan_edit.py::test_the_chosen_export_is_what_is_delivered_and_verified[choice2-.gif-size2-15-False]`. A 16:9 GIF comes out 712x400 and the test expects 960x540. The GIF sizing and that test came from different work. Decide which is right, keeping in mind that the Export tab tells the user what they will get.
- The skips: 2 in `test_agent_loop.py` (a hard-coded path to another checkout's demo file), 1 in `test_spoken.py` (`jfk.wav` not installed), and the real Keychain test, which is off by default.
- These failed under heavy load earlier and passed alone, so treat them as timing-sensitive: `test_close_keeps_workspace_lease_until_active_encoder_stops`, `test_cancelling_an_encode_stops_its_process_and_releases_worker`, `test_uploaded_clip_plans_and_streams_after_reload`, `test_timeout_stops_nested_children_even_when_parent_exits`. Prove a timing failure on a quiet machine before widening a deadline.

### 2. Run the Meta-connected Studio states live

These have only ever run against stubs in the page. Headless Chrome aborts inside the Codex sandbox (exit 134), so run them over HTTP against a real Studio, using the same routes the page calls. Start Studio from a shell that has loaded `.env.local`. Do not turn the sandbox off yourself. Keep a list of what could only be proven in a browser, and put it in your final report. Outside the sandbox, `scripts/shoot_studio.py` captures full-page screenshots (run it with `uv run --with websocket-client`).

- the "Direct the edit" note box
- Auto-refine from the render button
- the Cutout tab end to end, including "Generate a background"
- caption translation from the Captions tab, and confirm the burned text
- a multi-clip project in Meta mode

Meta Voice Transcribe in the current caption path has fixtures only; run it live once.

### 3. Fix these known defects

1. **Late Meta failure on long projects.** `meta.py` caps uploads at 200 MB and transcription at 10 minutes, but a long project only finds out after the proxy encode. Check before encoding.
2. **Voice gate cuts real speech.** In `voice_settings`, require a wider voice-to-room margin before the expander engages. Measured damage: 12.67% of loud windows on a long talk, 5.33% in a hall, 0% on a talking head. Remove the redundant `if margin < 8` block.
3. **Stabilize on already-stabilized footage.** No clear benefit on iPhone clips, worse on a whip pan. Do not leave it on by default: take it out of the `recap` template in `edit_spec.py`, and warn or skip when measured shake is low.
4. **Portrait into 16:9 single clip** center-crops to about 32% of the frame, while multi-clip uses a blurred fit. Use the blurred fit in both.
5. **Caption edits against Meta cues.** Preview cues come from the local transcript, so an edit may not match a Meta cue, and an unmatched edit is silently ignored. Either match them or tell the user.
6. **`.ts` sources do not play in browsers.** `plain_video` should normalize them.
7. **Transcripts skipped over 300 s at planning.** Long sources get no preview captions or filler removal. Find a better answer, such as background transcription.
8. **Look tables drift.** `preview.js` and `inspector.js` each hold a table of look previews. Use one.
9. **Dead code:** the `ctx.transcribe` branch in `render_plan.captions` is unused.
10. **Call-to-action cards** move in when a story shrinks but not when it grows.
11. **`scale2ref`** prints a deprecation warning in FFmpeg 9. Replace it if a supported alternative works on FFmpeg 6.1 too.

### 4. Close agent parity

`vedit mcp` has no tools for find, shorts, or script. Add them to `src/vedit/mcp_server.py` with tests.

### 5. Measure

- Run `uv run python scripts/op_costs.py <clip> <work-dir>` on a 20 s 1080x1920 clip and record the table in `docs/CAMPAIGN.md`.
- Rebuild the DMG on the final tree with `uv run vedit package dmg --out dist/Briefcut.dmg` and prove it launches. The last proof predates the final edits.
- Run the suite on Ubuntu (FFmpeg 6.1) if you can. CI's new steps have never run on GitHub.

### 6. Refresh the evidence and docs

- Rerun the real-clip baseline with `zsh scripts/real_baseline.sh <work-dir>`. Add the user's iPhone clips from `~/GitHub/gemini_editor/footage/hackathon` through `BRIEFCUT_CLIPS` (local only).
- Record fresh runs with `scripts/demo_run.py`, and make `docs/evidence/latest.md` the best honest run.
- Update `docs/CAPCUT_PARITY.md`, `docs/SUBMISSION.md`, `docs/HACKATHON.md`, `README.md`, and `CHANGELOG.md` to match. `docs/SUBMISSION.md` still cites older numbers, and its demo sequence should show the new editor.
- Check for leftover claims: `no Meta key`, `200 MB`, `one clip`.

## Limits to report, not hide

- Fillers: only what Whisper hears (2 of 21 said mid-flow).
- Word timing was measured on synthetic speech only. Human speech and non-English timing are unmeasured.
- Cutout: flyaway hair is lost, thin gaps keep slivers of background, and there is no manual correction tool.
- Emoji are removed from text and captions with a warning.
- Bundled music beds exist in `src/vedit/assets/music/` but no human has listened to them, so they are not offered. Leave them off until the user has.
- The Mac app does not bundle FFmpeg, is not notarized, and is Apple Silicon only.
- Linux captions need a manual whisper.cpp build.

## Decisions that belong to the user

Do not decide these. List them in your final report.

1. Whether to commit, merge to `main`, and push.
2. Which track to enter, and whether pre-existing code is allowed. The Official Rules were not on the public page when checked, so this is unconfirmed.
3. Whether to send their own footage to Meta in contributor mode for the demo.
4. Listening to the bundled music before it ships.
5. Recording the demo video. Their own footage will land better than the procedural clip.
6. Browser checks. The Meta-connected Studio states need a person or an unsandboxed run to see them in a real browser.
7. Leftover test data: `~/Library/Application Support/Vedit` holds a 4-second test project and three uploads (16 MB) from an app test, plus `~/Library/WebKit`, `Caches`, and `HTTPStorages` entries for `ai.aubric.briefcut`. Leave them for the user to remove.

## Done means

- The full suite passes on a quiet machine, and any skip is explained.
- Every item in section 2 ran live with its spend recorded.
- Real clips, including the user's local ones, pass through Studio's flow.
- Docs match the evidence, and the parity table is current.
- Your final report says plainly what is still weak.
