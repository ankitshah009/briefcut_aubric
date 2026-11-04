# Hero word behind the subject: live proof

Recorded 2026-11-04 on `examples/demo/workshop.mp4`, the procedural demo clip. Nothing else was sent to Meta.

The effect: for a moment, one huge spoken word sits behind the subject. The background is drawn first, then the word, then the cut-out subject on top. Outside that moment the video is unchanged.

## What each part did

| Step | Who | Result |
| --- | --- | --- |
| Hear the speech | whisper.cpp `ggml-base.en`, on this Mac | Word times for the first 5 s: "This is the finished oak bowl, oiled and ready for the table." No spend. |
| Pick the word | Muse Spark (`muse-spark-1.3-contributor`), strict JSON schema | `{"word": "oak", "start": 1.35, "end": 3.35, "side": "center", "reason": "It names the natural material of the hero wooden bowl."}` |
| Check the pick | `vedit.hero_word.validate_suggestion` | "oak" is said at 1.35 to 1.59 s, inside the one shot. The window opens 0.06 s early, at 1.29 s, and holds 2.00 s to 3.29 s. Nothing needed clamping. |
| Mask the subject | SAM 3.1, on a 2.00 s proxy of the window only | 60 frames sent, 60 masked (100%), area flicker 0.8%. Billed 60 frames. |
| Check the side | `vedit.hero_word.choose_side`, from SAM's mask | At Spark's "center", the bowl would cover 82% of the word (100% in 9:16). At "top", it covers 20% (28% in 9:16), so the word sits on the bowl's top edge, with the rim in front of the bottom of the letters. |
| Layer it | Briefcut's `hero_word` compile op | Two renders from the same matte: 1920x1080 and 1080x1920. |

The model proposed. Code checked the proposal against the transcript, the timeline and the mask, then the compiler did the work. Spark's side was overruled because the mask showed the word would not read there. The receipt says so.

## Meta calls and spend

| Operation | Model | Request id | Cost |
| --- | --- | --- | --- |
| `hero_word` | `muse-spark-1.3-contributor` | `resp_6aeb7c4fa8962bb2fd184399` | $0.0004346 (428 input, 1,959 output tokens) |
| `tracking` (masks) | `sam-3.1` | `resp_6aeb7c53bd65ab26e6804f56` | $0.0120 (60 frames) |
| **Total** | | | **$0.0124** of the $0.10 budget |

The project uses contributor mode, so Meta may train on what it received: transcript words of a procedural clip and a 2 s proxy of it. `evidence.json` was written by a later run that read both answers from the local cache, so its `usage` blocks say `cached: true`. The ledger in `meta_calls` holds the one real charge for each call.

## Proof from the files

Both renders were probed after encoding:

| Shape | Duration | Size | Audio | Video | Color |
| --- | --- | --- | --- | --- | --- |
| 16:9 | 5.0 s | 1920x1080 | yes | yes | bt709, tv |
| 9:16 | 5.0 s | 1080x1920 | yes | yes | bt709, tv |

Per-frame PSNR of the `hero_word` step against the tape it was given (150 frames, window is frames 39 to 98):

- Outside the window: at least 49.25 dB, mean 50.9 dB. That is the x264 re-encode at CRF 16, nothing else.
- Inside the window: 16.5 to 35.6 dB. The low end is the full word; the edges are the fade: 33.8 dB on frame 39, 35.6 dB on frame 98.
- The frames either side of the window, 38 and 99, measure 50.9 and 49.25 dB: the word does not leak out by a frame.

Mean luma difference against the same cut rendered without the word: 1.0 before, 15.2 inside, 1.0 after (0 to 255). The reference took a different path to the 16:9 frame (the reframe fuses into assembly when no layer needs the source picture), so a small difference outside the window is expected.

Receipts (`basis: derived`, `by: lint`, `inputs: ["sam-3.1", "muse-spark"]`), both passed:

- 16:9: "oak" for 2.00 s from 1.29 s; SAM masked 100% of 60 frames; the subject covers 20% of the word.
- 9:16: "oak" for 2.00 s from 1.29 s; SAM masked 100% of 60 frames; the subject covers 28% of the word.

The first live render used Spark's "center" and failed this receipt: the bowl covered 82% of the word. The contact sheet confirmed it. The side check was added from that result, and the rerun above used the same two paid answers.

## Files

- `contact-16x9.jpg`, `contact-9x16.jpg`: frames at 0.69 s (before), 2.29 s (inside) and 3.89 s (after).
- `evidence.json`: Spark's raw proposal and the checked one, SAM's matte report, the hero edit for each shape, the layout, the receipts, the observations, the ledger and the spend.
- `proof.py`: the script that made all of it. Rerun with the key loaded only for that command:

```sh
set -a; . ./.env.local; set +a
VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg uv run python docs/evidence/hero-word/proof.py WORK_DIR
```

## Limits

- One clip, one subject, one window. The subject is a static bowl on a plain table, the easy case for SAM. A person with hair or fast motion is untested here.
- The word is placed from the subject's average box over the window. A subject that moves a lot during the window can cover more of the word than measured at the start, middle and end frames.
- The layout assumes a centred crop, or the tracked crop when subject tracking is on. A zoom push after the hero word can trim its edges.
- Spark saw the transcript only, not frames. The side is checked against SAM's mask after the fact.
- A hero word written with combining marks is refused: Hindi "नमस्ते", Thai "ขอบคุณ" and vowelled Arabic all fail `edit_spec.HERO_WORD`, which has no combining marks. Unvowelled Arabic, CJK and accented Latin pass. `tests/test_review_features.py` keeps the failing case as a strict xfail until the pattern is fixed.
