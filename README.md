# Briefcut by Aubric.ai

Turn your footage into a story worth watching. Briefcut is a video editor built on Meta's models. Muse Spark watches your clips and proposes the story. SAM 3.1 follows the subject. Meta Voice Transcribe writes the transcript and captions. Muse Image makes the cover. You correct the plan, and FFmpeg renders the video on your own computer. Every version ships with receipts: a probe of the delivered file and the checks it passed or failed.

Briefcut is an Aubric.ai product. The Python package and CLI are called `vedit`; `vedit` and `briefcut` are the same tool.

## Which Meta models Briefcut uses, and where each runs

Every model Briefcut calls is a Meta model. All calls go through one adapter, [`src/vedit/meta.py`](src/vedit/meta.py), to the Meta Model API at `https://api.meta.ai/v1`, sent from your computer with your key. Models return data (ranges, boxes, cues, checks, patches). They never run code.

| Model | Model id in code | What it does in Briefcut | Where it runs | Source |
| --- | --- | --- | --- | --- |
| Muse Spark | `muse-spark-1.3`, or `muse-spark-1.3-contributor` in contributor mode | Plans the story from the footage and its transcript. Finds moments. Plans several shorts from one video. Turns a written script into scenes and picture prompts. Reviews the finished cut (Director's notes, checks, and patches). Turns a typed note into patches. Translates captions. | Meta Model API, called from this computer | `meta.py`: `plan_story`, `find_moments`, `plan_shorts`, `script_prompts`, `review_cut`, `patch_plan`, `translate_cues` |
| SAM 3.1 | `sam-3.1` | Follows a subject you name in words. Its boxes drive the 9:16 reframe, and its masks drive the cutout. | Meta Model API, called from this computer | `meta.py`: `track_subject`, `subject_masks` |
| Meta Voice Transcribe | `muse-voice-transcribe-1.0` | The only transcriber. It makes the planning transcript and the captions. | Meta Model API, called from this computer | `meta.py`: `transcribe_audio` |
| Muse Image | `muse-image-1.0` | Makes the cover from a real frame, generated backgrounds for cutouts, and the stills for script to video. | Meta Model API, called from this computer | `meta.py`: `generate_cover`, `generate_background`, `generate_image` |

<!-- VERIFY: planning transcript now comes from Voice Transcribe (Whisper removed). Confirm the call site in campaign.py and the 10-minute / 32 MB per-request limit for long sources. -->

These run on your computer and are not models:

- **FFmpeg** renders every frame of the delivered video.
- **Deterministic code** snaps cuts to sentence boundaries ([`story.py`](src/vedit/story.py)), runs the lint checks ([`lint.py`](src/vedit/lint.py)), applies patches, and compiles the edit to an FFmpeg program ([`compile.py`](src/vedit/compile.py)).

Prices, as encoded in `meta.py` (checked against <https://dev.meta.ai/docs/> on 2026-10-29): Muse Spark $1.25 in and $4.25 out per million tokens ($0.10 and $0.20 in contributor mode), SAM 3.1 $0.20 per 1,000 frames, Voice Transcribe $0.18 per hour of audio, Muse Image $0.01 per image.

### Private mode (not yet confirmed)

<!-- VERIFY: placeholder. A private mode that runs an open-weight Meta model (Muse Glimmer or Muse Spark Open) on this computer through llama.cpp, for text-only work (director's notes, translation), is being attempted. Do not publish any claim here until it has run. Fill in: model name and file, how to install llama.cpp, the env variable that turns it on, which features use it, and the evidence file. Delete this section if it does not ship. -->

Not available in this build.

## Judge quickstart

This works from a clean clone on macOS or Linux.

### What you need

- **uv**, which provides Python 3.13 and runs Briefcut. <https://docs.astral.sh/uv/>
- **FFmpeg** with the `drawtext` and `subtitles` filters. On a Mac that is Homebrew's `ffmpeg-full`. On Debian or Ubuntu, `apt-get install ffmpeg` has both.
- **A Meta Model API key** from <https://dev.meta.ai>. Your own key works. Briefcut never ships with one.

`bash scripts/install.sh` installs uv and FFmpeg for you. It prints a plan first and asks before each step. `bash scripts/install.sh --dry-run` shows the plan and changes nothing.

### Run it

```bash
git clone https://github.com/ankitshah009/briefcut_aubric && cd briefcut_aubric
bash scripts/install.sh          # skip if you already have uv and FFmpeg
uv sync
uv run vedit doctor              # every [required] row must pass
cp .env.example .env.local       # then paste your key after MODEL_API_KEY=
bash scripts/studio.sh --open
```

`.env.local` is in `.gitignore`. [`.env.example`](.env.example) explains each setting:

| Variable | Value | Meaning |
| --- | --- | --- |
| `MODEL_API_KEY` | your key | Sent only to `api.meta.ai`. |
| `VEDIT_META_ENABLED` | `1` | Allows Meta calls. Nothing is sent until you start a project. |
| `VEDIT_META_MODE` | `standard` or `contributor` | Contributor mode makes Muse Spark cheaper. In exchange, Meta may train on what you send to Muse Spark. See [Privacy](docs/PRIVACY.md#contributor-mode). |
| `VEDIT_FFMPEG` | optional path | The FFmpeg to use. `scripts/studio.sh` picks `ffmpeg-full` on a Mac when it is installed. |

<!-- VERIFY: exact env names and whether VEDIT_META_ENABLED stays in Meta-only mode. -->

You can also leave `.env.local` out: open Settings (the gear) in Studio, paste the key, and turn on **Allow Meta calls**. The key goes to the macOS Keychain when one is available, and to `~/.vedit/config.json` (mode 0600) otherwise. A variable in the environment wins over Settings.

`scripts/studio.sh` creates `.venv` with uv if needed, loads `.env.local`, runs `vedit doctor`, and opens Studio at `http://127.0.0.1:8765`. Studio listens only on your own machine.

### Check the key without spending

In Studio, press **Test connection** in Settings. From a terminal:

```bash
set -a; . ./.env.local; set +a
uv run vedit meta check
```

Both list the models your account can see. Neither buys inference. If your account cannot see one of the four models above, the check names it.

<!-- VERIFY: does `uv run vedit meta check` now read .env.local itself? Today only `briefcut` with no arguments and scripts/studio.sh load it. -->

### Make a first video

1. In Studio, choose `examples/demo/workshop.mp4` (40 s, in the repository) or your own clip.
2. Pick the "Product story, edited" chip, or type a brief, for example: "Make a 25-second vertical story: open with the finished bowl, then show how it is made. Trim the pauses, add a title card, fade out at the end."
3. Tick the box that confirms this footage may be sent to Meta, then press **Create story**. Muse Spark proposes the moments. Each one shows its reason and its source time.
4. Move, trim, reorder, add, or delete moments. Name a subject such as "the wooden bowl" if you want SAM 3.1 to keep it in frame.
5. Press **Make the video**. FFmpeg renders on your computer. The server probes the file before it marks the job done.
6. Read **The receipts**, then try **Apply director's fixes** or type a note. Version 2 renders and version 1 stays downloadable.

The demo clip is stylised computer-generated footage with a synthetic voice, not a recording. `uv run python scripts/make_demo.py` rebuilds it.

<!-- VERIFY: how make_demo.py produces the narration now that macOS `say` is removed from the runtime. The checked-in workshop.mp4 was built before that change. -->

### See every Meta call, and the receipts

- **Meta calls panel.** Studio lists every Meta call as it happens: the model id, what it was for, how long it took, what it cost, and Meta's request id. <!-- VERIFY: panel name, where it opens in Studio, and the exact columns. Built and browser-checked; not yet seen on a live Meta run. -->
- **The receipts.** After each render: the probe of the delivered file (duration, size, streams, color) and every check with a jump to its moment. Each check is tagged **Probed** or **Measured** (computed by code) or **Model opinion** (Muse Spark's review).
- **On disk.** Each project folder keeps `meta-usage.json` (every Meta reservation and its usage), `report.json` (the probe and checks), `brief.yaml`, `project.json`, and `render.sh`. The archive you download carries the video, cover, post copy, captions, `story.json`, `brief.yaml`, and `report.json`.

### What it costs

Each project has a spending cap: $0.25 by default, $5 at most, set in Settings. The adapter reserves the worst-case cost before each call and keeps the reservation after an uncertain failure, so retries cannot exceed the cap. In the recorded runs, a full loop on the 40 s demo clip with SAM tracking reserved $0.29 to $0.31; a run without SAM reserved about $0.01. SAM is the cost that grows with the cut's length. These are local estimates, not Meta's bill.

## No key? See recorded runs

You can see what Briefcut does without a key. These are records of real runs, written by `scripts/demo_run.py` or the same HTTP routes Studio calls. Each holds the request, the stage timings, the plan, the probe of the delivered file, the receipts, and the Meta ledger.

- [docs/evidence/latest.md](docs/evidence/latest.md) is the best honest run: Muse Spark plan, SAM 3.1 tracking, Voice Transcribe captions, Muse Image cover, Director's notes, and a typed note to version 2 (15 of 15 checks, then 13 of 15).

| Record | What it shows |
| --- | --- |
| [`20261031T083945Z.json`](docs/evidence/20261031T083945Z.json) | The run behind `latest.md`. |
| [`20261031T070719Z.json`](docs/evidence/20261031T070719Z.json) | The full loop with `--revise director`: 13 of 14 checks, then 12 of 13. $0.3131 reserved. |
| [`20261031T141719Z-auto-refine.json`](docs/evidence/20261031T141719Z-auto-refine.json) | Auto-refine: Spark failed 4 of 16 checks, applied 7 patches, and version 2 passed 16 of 16. Frames in [`auto-refine-v1-v2-frames.jpg`](docs/evidence/auto-refine-v1-v2-frames.jpg). |
| [`20261031T100101Z-note.json`](docs/evidence/20261031T100101Z-note.json) | A typed note ("warmer, bigger captions, slow down the oil moment") to a version 2 that passed 14 of 14. Frames in [`note-v1-v2-frames.jpg`](docs/evidence/note-v1-v2-frames.jpg). |
| [`20261031-live-cutout.json`](docs/evidence/20261031-live-cutout.json) | Cutout with a Muse Image background: SAM masked all 360 frames, 7 of 7 checks. |
| [`20261031-live-translation.json`](docs/evidence/20261031-live-translation.json) | Voice Transcribe captions and Spanish translation by Muse Spark, burned in: 10 cues in, 10 out. |
| [`20261031-live-multiclip.json`](docs/evidence/20261031-live-multiclip.json) | Two clips planned by Muse Spark into one 15 s edit, 5 of 5 checks. |
| [`20261031-script-to-video.json`](docs/evidence/20261031-script-to-video.json) | One Spark script call and 4 Muse Image stills became a 19.67 s video, 5 of 5 checks. Frames in [`script-to-video-frames.jpg`](docs/evidence/script-to-video-frames.jpg). |
| [`20261031T005236Z.json`](docs/evidence/20261031T005236Z.json) | SAM 3.1 tracking a red square across 300 frames. Frames in [`sam-tracking-frames.png`](docs/evidence/sam-tracking-frames.png). |

What these records do not show:

- They were recorded on 2026-10-31, before Briefcut became Meta-only. In them, the transcript sent with the planning prompt came from a local Whisper model, which has since been removed. The auto-refine and note runs also captioned with local Whisper. Script to video was narrated by a local macOS voice.
- All used contributor mode for Muse Spark, which lets Meta train on the submitted clip. No user footage was sent.
- The clips are generated demo footage, so they say nothing about quality on real footage.
- They came from the HTTP routes, not a browser session. The Meta calls panel did not exist yet.

More records, including older ones, are in [docs/evidence](docs/evidence/). [docs/SUBMISSION.md](docs/SUBMISSION.md) ties each claim to its record.

## What Briefcut does

Studio turns one or more clips or photos into an editable story and a downloadable campaign: video, cover, post copy, captions, and a verified render report. Planning and rendering run as durable jobs; closing the browser does not stop them.

Every cut snaps to a transcript sentence boundary. Editing phrases in the brief ("trim the pauses", "add a title card", "fade out at the end", `hook:`, `cta:`) become engine ops and are listed as decisions on the plan. After the render, the receipts list the probe of the file and the checks that passed or failed: automatic checks from `src/vedit/lint.py` (sentence boundaries, duration, bounds, caption coverage, requested edits) and, with review on, Director's notes from Muse Spark with its own checks and proposed fixes. "Apply director's fixes", "Apply automatic fixes", or a typed note render the next version. The previous verified version stays downloadable under its own tab with its own report.

Studio takes up to 30 videos and photos, up to 4 GB each and 2 hours in all, plus audio and images for music and overlays. Meta's own limits still apply to what is sent: 200 MB per upload and 10 minutes of audio per transcription request. Briefcut checks these before it encodes the proxy.

<!-- VERIFY: how Meta-only planning handles sources longer than 10 minutes of speech. -->

Floating frame, Perspective sway, and Push-in reveal add curated motion inside the same flow. They animate a flat video plane; they are not 3D reconstruction. See [spatial effects](docs/SPATIAL_EFFECTS.md).

Social publishing is not built. The delivery is a downloadable archive. See [setup, API, and recovery](docs/CAMPAIGN.md), the [hackathon demo guide](docs/HACKATHON.md), and [what is sent to Meta](docs/PRIVACY.md).

## Use Briefcut from any MCP agent

`vedit mcp` serves the same project manager as Studio over MCP on stdio, so any MCP client can drive it: Claude Code, Codex, Cursor, Gemini CLI, and others. An agent can import clips, plan a story, edit and render it, find moments, make shorts, turn a script into a video, and read the receipts and artifact paths. [MCP for coding agents](docs/CAMPAIGN.md#mcp-for-coding-agents) lists the tools. Every route needs uv and FFmpeg, and Meta features need the key set up as above.

**This repository.** [`.mcp.json`](.mcp.json) defines a `briefcut` server that runs `uv run --no-dev --extra mcp --extra sam vedit mcp` in the repository root. Clients that read a project `.mcp.json`, such as Claude Code, offer it when you open the checkout.

**Any client, by hand.** From a checkout:

```bash
claude mcp add briefcut -e VEDIT_WORKSPACE=/path/to/workspace -- uv run --directory /path/to/briefcut_aubric vedit mcp
codex mcp add briefcut --env VEDIT_WORKSPACE=/path/to/workspace -- uv run --directory /path/to/briefcut_aubric vedit mcp
```

Cursor reads this block from `~/.cursor/mcp.json`, and Gemini CLI from `~/.gemini/settings.json`:

```json
{
  "mcpServers": {
    "briefcut": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/briefcut_aubric", "vedit", "mcp"],
      "env": { "VEDIT_WORKSPACE": "/path/to/workspace" }
    }
  }
}
```

After `bash scripts/install.sh`, which installs the MCP server with Briefcut, `claude mcp add --scope user briefcut -- "$(uv tool dir --bin)/briefcut" mcp` works too.

**Claude Code plugin.** The repository is also a plugin marketplace. The plugin adds the `briefcut` server and the [Briefcut skill](skills/briefcut/SKILL.md), which teaches the agent the flow, the proof rule, and when footage may go to Meta.

```text
/plugin marketplace add ankitshah009/briefcut_aubric
/plugin install briefcut@briefcut
```

If the `briefcut` tools do not appear in `/mcp`, restart Claude Code. The first start installs Briefcut's Python packages into the plugin's folder, so it needs network access once. Use the plugin or `.mcp.json`, not both.

**Where projects go.** In `VEDIT_WORKSPACE` when it is set, otherwise under `vedit-sessions/` in the folder the server starts in. In a git repository, add `vedit-sessions/` to `.gitignore`. One process at a time can work on a workspace. While Studio or the Mac app has it open, the agent's calls fail with "Another Studio is already processing this workspace". Close the other one, or give the agent a different `VEDIT_WORKSPACE`.

`briefcut_edit` needs `allow_meta` before a change sends captions or footage to Meta, and `briefcut_generate_background` needs `consent`. The skill tells the agent to send footage only when you chose Meta for that project. See [Privacy](docs/PRIVACY.md).

<!-- VERIFY: which MCP tools still take a provider argument now that local mode is gone. -->

## The Mac app

`uv run vedit package dmg --out dist/Briefcut.dmg` builds `Briefcut.app` and a disk image. The app opens the same editor in a native window, and downloads open a native Save dialog. It runs on Apple Silicon with macOS 15 or later, with bundle ID `ai.aubric.briefcut`. It carries its own Python, copied from uv's relocatable CPython 3.13 (`--no-python` leaves it out).

- **FFmpeg is not in the app.** Its first-run panel gives the command: `brew install ffmpeg-full`.
- **The build is not notarized.** A DMG built on your own Mac opens normally. One downloaded from elsewhere is blocked by macOS until you run `xattr -dr com.apple.quarantine /Applications/Briefcut.app` once.
- **Add the key in Settings.** That is the simplest route in the app.

The app and `briefcut` share the `~/Library/Application Support/Vedit` workspace, so both show the same projects. Only one can run on a workspace at a time. `VEDIT_WORKSPACE`, or the project folder in Settings, moves it.

## Architecture

```text
browser (src/vedit/studio/static)        talks only to 127.0.0.1
   │  upload once · brief · plan · edit · approve · receipts · revise · download
   ▼
HTTP server (src/vedit/studio/server.py)
   ▼
CampaignManager (src/vedit/campaign.py)  SQLite job record · one worker · cancel · retry · versions
   ├── Meta adapter (src/vedit/meta.py)          Spark · SAM 3.1 · Voice Transcribe · Muse Image
   │        spending cap · schema validation · upload cleanup · ledger
   ├── Story (src/vedit/story.py)                scene cuts, speech ranges, snap_to_sentences
   ├── Brief ops (src/vedit/brief_ops.py)        "trim the pauses", "title card", "fade out" → ops
   ├── Lint (src/vedit/lint.py)                  deterministic checks and patches; apply_patches is pure
   ▼
approved plan → YAML brief → compiler (src/vedit/compile.py) → project.json + render.sh
   ▼
FFmpeg on this computer → probe → report.json → checks → campaign.zip
```

The boundary that matters: models propose moments, geometry, cues, and patches. The compiler is the only thing that produces executable steps, and the lint re-runs after every model patch. `render.sh` in the project folder re-runs the edit with FFmpeg alone, and `brief.yaml` recompiles it with `vedit`.

```text
intent (YAML brief | English DSL | Python API)
        ↓
   analysis (silence, scenes, loudness, speech)
        ↓
   timeline program (project.json + render.sh)
        ↓
   FFmpeg
        ↓
   verified outputs + report.json
```

The engine follows the open-source tools that last: [MLT](https://www.mltframework.org/) / Shotcut / Kdenlive (tractor and playlist), [OpenTimelineIO](https://opentimeline.io/) (interchange), [editly](https://github.com/mifi/editly) (JSON to FFmpeg), and [auto-editor](https://github.com/WyattBlue/auto-editor) (silence-first cutting). `vedit` takes the IR and code generation idea and makes the program the product.

## The engine

| Job | How |
|---|---|
| Cut / trim / concat / speed / reverse / freeze | FFmpeg trim, concat, setpts |
| Remove silence (jump cut) | `silencedetect` → keep ranges → `select`/`aselect` |
| Captions | Meta Voice Transcribe → polished sentence cues, two lines of at most 42 characters → SRT/ASS → burned in by FFmpeg. A finished `.srt` or `.ass` also works through `captions` with `file:`. |
| Story plan checks | `lint.py`: sentence boundaries, duration vs target, bounds and order, caption coverage; patches applied by a pure function |
| Titles, watermarks, PIP | `drawtext` / `overlay` (ffmpeg-full) |
| Music duck under speech | `sidechaincompress` |
| Loudness | two-pass EBU R128 `loudnorm` |
| Color / denoise / sharpen / blur / chroma key | `eq`, `lut3d`, `hqdn3d`, `unsharp`, `boxblur`, `chromakey` |
| Aspect convert | fit (letterbox) or fill (center crop) to 16:9 / 9:16 / 1:1 / 4:5 |
| Subject reframe and cutout | SAM 3.1 boxes and masks, applied by `track_reframe` and the cutout ops |
| 3D-style footage effects | curated perspective motion with bounded strength and timing; `spatial_effect` op |
| Slideshow | stills + `xfade` |
| Transitions | `xfade` + `acrossfade` |
| Export | YouTube 1080p/4K, Shorts, preview, podcast audio, mezzanine |
| Hardware encode | `h264_videotoolbox` on Apple Silicon (never for `web_preview`); `--no-hw` forces libx264; a VideoToolbox failure retries in software |
| Color | bt709 / tv range / yuv420p / `+faststart` / High profile on delivery presets |
| Interchange | `timeline.otio.json` (OTIO-lite) next to `project.json` |

<!-- VERIFY: how the brief-level `captions` op transcribes in Meta-only mode (vedit do --captions, recipes). -->

Every run writes:

```text
out/<name>/
  brief.yaml            # what you asked for
  project.json          # compiled step graph
  timeline.otio.json    # OTIO-lite interchange
  render.sh             # standalone, re-runnable
  steps/*.sh            # one script per step
  report.json           # durations, sizes, streams, color
  *.mp4 / *.mp3         # outputs
```

A YouTube brief is two video encodes (silence cut, then one master with captions, fade, loudness, preset, and broadcast tags), plus verify. `vedit plan` writes scripts only. `vedit render DIR` runs a planned work dir, and `--resume` skips finished steps.

### Tell it what to do

```bash
vedit do talk.mp4 --cut-silence --music bed.mp3 --preset youtube_1080p
vedit do "cut silence from talk.mp4; duck bed.mp3; reframe 9:16; export shorts"
vedit plan examples/youtube.yaml --work-dir /tmp/yt
vedit execute /tmp/yt                      # run the planned program; no recompile
vedit render --resume --work-dir /tmp/yt   # skip steps whose outputs already exist
vedit recipe list
vedit recipe apply slideshow --images 01.png 02.png 03.png --music bed.mp3
```

```python
from vedit import Edit

Edit("talk.mp4").cut_silence().music("bed.mp3").loudnorm().to("out/final.mp4")
```

`vedit agent "goal" clip.mp4` writes a session (intent, inventory, decisions, `brief.yaml`, `project.json`, `render.sh`) and renders it; `--plan` stops at the program. `vedit tools` is that loop as stdio JSON. Its English parser is deterministic code and calls no model.

### Brief language

```yaml
name: podcast-episode
canvas: { width: 1920, height: 1080, fps: 30, sample_rate: 48000 }
sources:
  talk: ./interview.mp4
  music: ./bed.mp3
ops:
  - { op: cut_silence, threshold_db: -34, min_silence: 0.5, pad: 0.14 }
  - { op: highpass, hz: 80 }
  - { op: music, source: music, duck: true, volume_db: -18 }
  - { op: loudnorm, i: -16 }
  - { op: export, presets: [podcast_video, podcast_audio] }
```

Ops: `trim`, `cut_silence`, `concat`, `speed`, `reverse`, `freeze`, `fit`, `fill`, `scale`, `crop`, `pad`, `rotate`, `hflip`, `vflip`, `fps`, `color`, `denoise`, `sharpen`, `blur`, `fade_in`, `fade_out`, `volume`, `highpass`, `lowpass`, `loudnorm`, `title`, `captions`, `overlay`, `watermark`, `pip`, `music`, `replace_audio`, `extract_audio`, `reframe`, `slideshow`, `thumbnail`, `chromakey`, `scenes`, `export`.

## Develop and test

uv owns the environment; do not use `python -m venv` or bare `pip`.

```bash
uv sync                        # .venv on CPython 3.13 with vedit and the test tools
export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
uv run vedit doctor
uv run python scripts/make_demo.py   # rebuilds the clips under examples/demo/
uv run pytest -n auto
```

Tests run in parallel with pytest-xdist, in CI too. The Meta tests stub the network call, so the suite needs no key.

<!-- VERIFY: suite totals after the Meta-only change. -->

## What this is not

Not a Premiere or Resolve clone, and not a black-box AI editor. The models propose; you approve; the compiler renders; the receipts show what came out. Briefcut sends no analytics and has no account of its own. At runtime, the only service it calls is the Meta Model API. See [Privacy](docs/PRIVACY.md).
