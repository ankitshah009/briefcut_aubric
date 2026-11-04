# Verified find: Muse Spark finds, SAM 3.1 checks

Live run on 2026-11-04 against `examples/demo/workshop.mp4` (a 40.0 s procedural clip: finished oak bowl, raw blank, turning with a gouge and shavings, sanding, oil). No other footage was used.

A schema can guarantee format but not correctness. Muse Spark's `find` always returns moments that parse. `vedit.verify_find.verify_moments` asks a second model whether the named thing is really in the picture there. Each found moment gets one 3 s window from its middle, sampled at 15 fps (45 frames, inside SAM's 50-frame minimum bill, so $0.01 per moment). SAM 3.1 masks the noun phrase. The evidence score is the share of sampled frames with a mask over 0.1% of the picture.

## Models and spend

| Call | Model | Request id | Size | Cost (actual) |
|---|---|---|---|---|
| Find "the wooden bowl" | muse-spark-1.3-contributor | resp_6aeb7ca70eff0caca67a486a | 11,040 in / 1,090 out tokens | $0.00132 |
| Check "wooden bowl", 2.5-5.5 s | sam-3.1 | resp_6aeb7cb052c498883bcb4281 | 50 frames billed | $0.01 |
| Check "wooden bowl", 26.5-29.5 s | sam-3.1 | resp_6aeb7cb072083ab0a1e943b6 | 50 frames billed | $0.01 |
| Find "the gouge" | muse-spark-1.3-contributor | resp_6aeb7cd01db2bb4822174e90 | 11,040 in / 1,612 out tokens | $0.00143 |
| Check "gouge", 22.5-25.5 s | sam-3.1 | resp_6aeb7cd9c21a47d7553146fa | 50 frames billed | $0.01 |
| Find "a dog" | muse-spark-1.3-contributor | resp_6aeb7ced3d320fe86f2e4df1 | 11,039 in / 667 out tokens | $0.00124 |
| Check "dog" (injected), 2.5-5.5 s | sam-3.1 | resp_6aeb7cf67a66debb75394098 | 50 frames billed | $0.01 |
| Check "dog" (injected), 26.5-29.5 s | sam-3.1 | resp_6aeb7cf6fcf8e33250644879 | 50 frames billed | $0.01 |
| Check "tool" (phrase probe), 22.5-25.5 s | sam-3.1 | resp_6aeb7d7b994278f2fbc24a37 | 50 frames billed | $0.01 |
| Check "gouge" at 720p (probe), 22.5-25.5 s | sam-3.1 | resp_6aeb7da6cc5d9e475d1f4e88 | 50 frames billed | $0.01 |

Total: **$0.0740 actual** ($0.0799 held by reservations) against a **$0.15** ledger cap that every call shared. SAM calls took 4.9 to 8.4 s each and run in parallel per query. A repeat run is free: every Spark and SAM answer comes back from the project cache (a full warm rerun of this proof takes 7.8 s).

## What was kept, flagged or dropped, and why

| Query | Muse Spark found | SAM 3.1 coverage | Result |
|---|---|---|---|
| the wooden bowl | 0.0-8.0 s "An empty wooden bowl sits centered on dark planks." | 45 of 45 frames (100%), median confidence 0.97, mask 23% of the picture | **verified** |
| the wooden bowl | 16.0-40.0 s "The wooden bowl stays centered while a tool touches its edge." | 45 of 45 frames (100%), confidence 0.98 | **verified** |
| the gouge | 16.0-32.0 s "A gray gouge tool contacts the wooden bowl rim with shavings scattering." | 0 of 45 (0%), SAM returned empty text | **flagged** (kept, ranked last, receipt fails) |
| a dog | nothing | not run | nothing to check; Spark was right |
| a dog (injected) | Spark's two bowl moments, offered as "a dog" | 0 of 45 and 0 of 45 | **flagged**; with `unseen="drop"` both are dropped |

Muse Spark returned no dog at all, so SAM had nothing to reject. To run the rejection path live, the proof hands SAM Spark's two bowl moments under the query "a dog". That injected pair is labelled as such in `results.json` (`"injected": true`). SAM found no dog in any of the 90 frames.

The gouge is the important result. Muse Spark was right: the gouge is plainly visible in that window (every fifth sampled frame was checked by eye; see `thumbs/gouge-22500-unmasked.jpg`). SAM 3.1 returned empty text for "gouge". Two $0.01 probes rule out the obvious causes. The generic phrase "tool" also got nothing (`thumbs/tool-22500-unmasked.jpg`), and the same window at the source's full 720p instead of 360p got nothing too (`probe-720p.json`). This clip's gouge is a flat gray rounded bar drawn by code. SAM 3.1 does not recognise it as a gouge or a tool. That is a false negative from SAM, not a hallucination from Spark.

That is why the default is **flag, not drop**. A dropped moment is a correct result the user never sees. A flagged moment stays in the list, ranked after verified ones, with `verified: false`, `coverage: 0`, a note, and its Spark score halved (the original is kept as `evidence.spark_score`) so any later ranking by score keeps SAM's order. `unseen="drop"` is one parameter away. Even then, dropped moments are kept under `dropped` with the reason, so a UI can offer to show them.

The bowl thumbnails show the mask is real segmentation, not a box: in `thumbs/wooden-bowl-26511.jpg` the outline notches around the gouge where it crosses the rim.

## Files

- `results.json`: every query's Muse Spark moments, the annotated result (`moments`, `dropped`, `spend`, `receipt`), the call log with request ids, and totals.
- `probe-720p.json`: the gouge window again at full resolution.
- `thumbs/`: one frame per checked moment. A green outline and light fill mark SAM's mask on the frame where it was largest. `-unmasked` frames had no mask and show the middle sampled frame.
- `contact-sheet.jpg`: all thumbnails labelled with query, window, coverage and outcome.
- `reproduce.py`: the exact script. Run `set -a; . ./.env.local; set +a; uv run python docs/evidence/verified-find/reproduce.py WORK_DIR` from the repository root.

## Receipt shape

Each result carries one record shaped like the checks in campaign receipts:

```json
{"name": "sam: “wooden bowl” is on screen in the found moments", "passed": true, "at": null,
 "note": "SAM 3.1 looked for “wooden bowl” in 2 moments Muse Spark found: 2 verified (coverage 100%, 100%).",
 "basis": "derived", "by": "sam-3.1", "inputs": ["muse-spark"]}
```

## Limits

- SAM 3.1 misses things it has no good name for. On this clip it missed the stylised gouge under two names and two resolutions. Real footage of a real gouge may do better; this run does not show that.
- The noun phrase comes from a fixed rule, not a model, so unusual wording can produce a poor phrase. Muse Spark already describes what it saw ("A gray gouge tool..."), and having it return a SAM phrase in the same structured call would be better. That needs a schema change in `meta.find_moments`.
- One 3 s window per moment checks the middle of the moment, not all of it. A long moment where the thing appears only at one end can score low.
- Coverage counts SAM's main object in each window (the identity `meta.subject_masks` selects). If SAM re-identifies the object partway through, coverage can read low.
- The call log labels these SAM calls "Cut out ..." because they go through `meta.subject_masks`, which sets that purpose.
