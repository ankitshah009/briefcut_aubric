# Long sources: live proof (2026-11-04, standard mode)

A source longer than one Meta call can watch (10 minutes) is planned in windows. Muse Spark watches each
window and logs candidate moments on the source clock. One text-only editor call reads the whole transcript
and every candidate, and picks the story by candidate id. Code checks every id, takes the times from the
candidates (never from the answer), then snaps to sentences and validates the plan as for any project.

Reproduce from the repository root (the key comes from the Keychain through Briefcut's settings):

```text
export VEDIT_META_MODE=standard VEDIT_META_ENABLED=1 VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
uv run python docs/evidence/long-form/tears-of-steel/reproduce.py WORK_DIR FILM OUTPUTS   # Run 2: the film
uv run python docs/evidence/long-form/procedural/reproduce.py WORK_DIR                    # Run 1: procedural
uv run python docs/evidence/long-form/procedural/editor_v2.py WORK_DIR                    # Run 1: editor after the prompt fix
```

## Long view

The editor never watches the video. It reads the transcript and the candidates as text, so its input stays
small however long the source is. Counted is what Briefcut reserved against; billed is what Meta charged.

| Editor call | Counted input | Billed input | Reserved | Share of the 1,048,576-token ceiling | Transcript | Candidates |
| --- | --- | --- | --- | --- | --- | --- |
| Tears of Steel trailer (12:14) | 3,208 | 2,937 | $0.0736 | 0.31% counted, 0.28% billed | 337 words | 24 |
| Tears of Steel shorts | 2,733 | 2,428 | $0.0730 | 0.26% counted, 0.23% billed | 337 words | 17 left after the trailer |
| Procedural story (15:04) | 6,481 | 6,210 | $0.0777 | 0.62% counted, 0.59% billed | 2,155 words | 22 |
| Procedural shorts | 5,946 | 5,641 | $0.0771 | 0.57% counted, 0.54% billed | 2,155 words | 14 left after the story |

Scaled to the 2-hour project limit at the procedural run's rate of speech (about 17,000 words) and 12 windows
of up to 12 candidates, the editor would read roughly 40,000 tokens, under 4% of the ceiling. The outline it
receives is capped at 120 KB, which holds about two hours of speech.

## Run 2: Tears of Steel (`tears-of-steel/`)

Tears of Steel (2012). **(CC) Blender Foundation | mango.blender.org**, Creative Commons Attribution 3.0
(logos excluded). File `tears_of_steel_1080p.mov`: **734.2 s (12:14), 1920x800, 24 fps, with sound**, live action
with English dialogue, end credits included. Over the 10-minute single-call limit, so it runs in windows.

Goal: "A 60-second trailer: hook first, the stakes, the turn, no spoilers of the ending". Then 3 shorts of 15 to 30 s.
Trailer project budget $0.80; each short $0.10.

### Result

| Step | What happened | Wall time |
| --- | --- | --- |
| Hear | Meta Voice Transcribe, 2 pieces (556 s + 177 s), 83 lines, 337 words | 89 s |
| Windows | 2 windows, read in parallel: 0:00 to 6:06 and 6:06 to 12:14 | |
| Candidates | Muse Spark, one call per window: 12 + 12 = 24 candidate moments | 36 s, 57 s |
| Editor | Muse Spark, text only: picked 5 of the 24 by id | 14 s |
| Plan total | transcription, measurement, 2 proxies, 3 Meta calls | 297 s |
| Trailer render | 16:9, captions cut from the plan's transcript (no new Meta call) | 14 s |
| Shorts | 3 shorts from the same 24 candidates, one text-only call, each rendered 9:16 | 156 s |

The trailer picks, in order: w1-1 the launch countdown ("We have main engine start, four, three, two, one."),
w1-2 the break-up on the bridge, w1-9 "Tom, there she is... be tender to her", w1-12 "that's no reason to destroy
the world", and w2-8 "Captain, we have to abort." It left out the late candidates that give the ending away
(w2-10 "The world's changed, Celia", w2-12 the closing lesson), as the goal asked.

Every export, probed after the render (`summary.json`, `exports`):

| Export | Length | Size | Video, audio | Checks |
| --- | --- | --- | --- | --- |
| Trailer, 16:9 | 44.38 s | 1920x1080 | yes, yes | 5 of 5 |
| Short 1 "We Already Tried That" (6:07 to 6:32) | 23.50 s | 1080x1920 | yes, yes | 5 of 5 |
| Short 2 "Ten Minutes to Action" (3:11 to 5:05) | 26.25 s | 1080x1920 | yes, yes | 5 of 5 |
| Short 3 "Sorry, Not My Fault" (6:37 to 7:00) | 21.29 s | 1080x1920 | yes, yes | 5 of 5 |

The trailer is 44 s, not 60: the target is a ceiling, and the editor stopped when the story was told. Each short
is 15 to 30 s. No short overlaps another short or the trailer. Copies for the film are in
`~/Movies/Briefcut-demo/outputs/long-form/` (`tears-of-steel-trailer-60s-16x9.mp4`, `tears-of-steel-short-1-9x16.mp4`,
`-2-`, `-3-`).

### Spend

| Call | Model | Reserved | Billed tokens in / out | Cost |
| --- | --- | --- | --- | --- |
| Transcribe 556 s | muse-voice-transcribe-1.0 | $0.0279 | | $0.0278 |
| Transcribe 177 s | muse-voice-transcribe-1.0 | $0.0089 | | $0.0089 |
| Candidates, 0:00 to 6:06 | muse-spark-1.3 | $0.2191 | 84,625 / 3,095 | $0.1189 |
| Candidates, 6:06 to 12:14 | muse-spark-1.3 | $0.2188 | 83,510 / 5,310 | $0.1270 |
| Editor, trailer | muse-spark-1.3 | $0.0736 | 2,937 / 2,080 | $0.0125 |
| Editor, 3 shorts | muse-spark-1.3 | $0.0730 | 2,428 / 9,647 | $0.0440 |

**Total: $0.3391** ($0.2951 for the trailer, $0.0440 for the shorts; their renders made no Meta call).
Wall time for the scripted run: 467 s (7.8 minutes), with the two windows read at once.

Before anything was sent, Briefcut estimated the trailer's plan at **$0.40** expected ($0.56 at most).
It cost $0.30. Meta billed about 229 video tokens a second; the estimate prices 320.

### What the contact sheet shows

`tears-of-steel/contact-sheet.jpg` has one labelled frame from the middle of every picked moment. I looked at it.

- Trailer 1 (0:01) is a black title card, "the Mango Open Movie project": Muse Spark chose the moment for the
  launch countdown heard over it, so the trailer opens on sound, not picture.
- Trailer 2 to 5 are the bridge break-up, the scientists in the lab, Tom facing Celia, and the abort call:
  setup, stakes, plea and turn, in film order, none from the resolution.
- The shorts are distinct scenes: the holographic control room and the robot arm (short 1), the lab crew and the
  big robot (short 2), the man with the eyepiece and a confrontation by the canal (short 3).
- A frame from the exported trailer shows the picture filled to 16:9 with the caption
  "Look, Celia, we have to follow our passions." burned in; a short shows a clean 9:16 centre crop.

## Run 1: procedural source (`procedural/`)

### Source

`examples/demo/workshop.mp4` (40 s) and `examples/demo/talk.mp4` (3 s), both procedural, scaled to 1280x720 and
joined 21 times: **903.7 s (15:04), 1280x720, with sound.** No user footage.

### Result

| Step | What happened | Wall time |
| --- | --- | --- |
| Hear | Meta Voice Transcribe, 2 pieces (576 s + 329 s), 388 lines, 2,155 words | 161 s |
| Windows | 2 windows cut at sentence edges: 0:00 to 7:34 and 7:34 to 15:04 | |
| Candidates | Muse Spark, one call per window: 12 + 10 = 22 candidate moments | 33 s + 30 s |
| Editor | Muse Spark, text only: picked 8 of the 22 by id | 23 s |
| Plan total | transcription, measurement, 2 proxies, 3 Meta calls | 556 s |
| Render | 60 s target, 16:9, captions from the plan's transcript (no new Meta call) | 24 s |
| Shorts | 3 shorts of up to 30 s, from the same 22 candidates, one text-only call | 119 s |

The rendered story, probed: **54.29 s, 1920x1080, has_video true, has_audio true**, 6 of 6 checks passed.

The shorts (`procedural/shorts.json`), all on the source clock and none overlapping each other or the story:

| Short | Length | Moments |
| --- | --- | --- |
| Fallen Tree to Finished Oak Bowl | 21.8 s | 474.6-481.9, 490.7-497.9, 506.5-513.8 |
| Wet Blank to Glowing Grain | 25.6 s | 560.8-572.2, 585.1-592.2, 593.0-600.0 |
| Grain to Glow Finish | 21.5 s | 482.4-489.2, 498.6-506.0, 863.0-870.4 |

### Spend

Every call is on a project ledger (`procedural/meta-calls.json`). The story project's budget was $0.60; each short's $0.10.

| Call | Model | Reserved | Billed tokens in / out | Cost |
| --- | --- | --- | --- | --- |
| Transcribe 576 s | muse-voice-transcribe-1.0 | $0.0289 | | $0.0288 |
| Transcribe 329 s | muse-voice-transcribe-1.0 | $0.0165 | | $0.0165 |
| Candidates, 0:00 to 7:34 | muse-spark-1.3 | $0.2587 | 89,302 / 4,314 | $0.1300 |
| Candidates, 7:34 to 15:04 | muse-spark-1.3 | $0.2573 | 89,199 / 2,591 | $0.1225 |
| Editor, story | muse-spark-1.3 | $0.0777 | 6,210 / 3,187 | $0.0213 |
| Editor, 3 shorts | muse-spark-1.3 | $0.0771 | 5,641 / 3,977 | $0.0240 |
| Editor, story, v2 prompt | muse-spark-1.3 | $0.0778 | 6,256 / 1,024 | $0.0122 |

**Total: $0.3552** ($0.3190 for the plan and render, $0.0240 for the shorts, $0.0122 for the v2 editor call).
Wall time for the scripted run: 698.5 s (11.6 minutes).

Before anything was sent, Briefcut estimated the plan at **$0.48** expected ($0.63 at most). It cost $0.32.
The estimate prices video at the 320 tokens a second the ledger reserves; Meta billed about 197 a second here,
so the estimate runs high, and a budget it accepts is never too small.

### What the contact sheet shows

`procedural/contact-sheet.jpg` has one labelled frame from the middle of every picked moment. I looked at it.

- Story 1 to 5 are the five beats of the workshop in order: the finished bowl, the raw blank, the lathe cut,
  sanding, the oiled bowl. They all come from the first 1:22, the first full pass of the loop.
- Story 6 to 8 repeat the lathe, sanding and oil beats from the second pass. The source repeats the same
  43 s 21 times, and the first editor prompt asked to "aim close" to the 60 s target, so it padded with repeats.
- Fix, in this change: the editor prompt now says the length is a ceiling and no two moments may show or say the
  same beat (the shorts prompt too). `procedural/editor-v2.json` is the same call on the same 22 candidates after the fix:
  it picked w1-1 to w1-5, the five beats once each, 33.9 s, with no repeat. The rendered video is from v1.
- The shorts come from the second window (7:55 to 10:00, and 14:23). Code kept every short off the story's
  moments and off each other. Short 3 opens on the raw blank, but shorts 1 and 2 both open on the
  finished-bowl line, which this source says 21 times. The shorts prompt now forbids a beat repeated across
  shorts as well; that change was not run live again.

## Files

Each run folder holds the same set. `tears-of-steel/` ran after the editor prompt fix found in Run 1.

- `plan.json`: the planned story or trailer, with the whole transcript in `analysis`.
- `candidates.json`: every candidate, its window, and each window call's usage.
- `shorts.json`: the three short plans.
- `meta-calls.json`: every ledger entry and call-log row for the four projects.
- `summary.json`: the numbers above, wall times, and each export's probe.
- `contact-sheet.jpg`: one frame per picked moment.
- `reproduce.py`: the script that made the folder.
- `procedural/editor-v2.json` and `procedural/editor_v2.py`: Run 1's editor call after the prompt fix.

Tears of Steel: (CC) Blender Foundation | mango.blender.org
