# Briefcut by Aubric.ai

Turn your footage into a story worth watching. Briefcut is a local video editor
with editable story cuts, captions, optional Meta assistance, and curated
3D-style effects. It is an Aubric.ai product, powered by the `vedit` editing engine.

The Python package, CLI command, and existing workspace paths remain `vedit` so
existing projects and scripts keep working. The proposed repository name is
`briefcut`; see [brand and repository setup](docs/BRAND.md).

## Campaign Studio

Studio turns one clip into an editable story and a downloadable campaign: video,
cover, post copy, captions when a local speech model is available, and a verified
render report. Planning and rendering run as persistent jobs; closing the browser
does not interrupt them. Review and change the selected moments before rendering.

```bash
bash scripts/studio.sh --open
```

The launcher uses the checkout's virtual environment and finds the optional local
Whisper model at `.vedit-models/ggml-base.bin`. Local mode works without an account
or model API. It starts with an explicitly labeled, editable opening cut.

Optional Meta mode adds Muse Spark story planning, SAM subject tracking, and Muse
Image covers. It requires server-side credentials and explicit enablement. A
campaign spending cap includes uncertain attempts; retries do not reset it.
Captions and final video rendering stay local. No model calls happen simply by
opening Studio.

Add Floating frame, Perspective sway, or Push-in reveal inside the same editing
flow. Control strength and timing; the effects render locally before captions.
See [spatial effects](docs/SPATIAL_EFFECTS.md) for examples and their limits.

See [setup, API, and recovery](docs/CAMPAIGN.md) and the
[hackathon demo guide](docs/HACKATHON.md). Social publishing is not implemented;
the delivery is a downloadable campaign package. Live provider verification
requires configured Meta access; HTTP fixture tests alone do not prove it.

```text
intent (YAML brief | English DSL | Python API)
        ↓
   analysis (silence, scenes, loudness, speech)
        ↓
   timeline program (project.json + render.sh)
        ↓
   FFmpeg (ffmpeg-full on this Mac)
        ↓
   verified outputs + report.json
```

This is the architecture used by the open-source tools that actually survive: [MLT](https://www.mltframework.org/) / Shotcut / Kdenlive (tractor + playlist), [OpenTimelineIO](https://opentimeline.io/) (interchange IR), [editly](https://github.com/mifi/editly) (JSON → FFmpeg), [auto-editor](https://github.com/WyattBlue/auto-editor) (silence-first cutting). vedit takes the IR + codegen idea and makes the **program** the product, not a GUI.

## Agent

`vedit agent` writes a session from a goal and a media file, then renders. `--plan` stops at the program.

```text
vedit agent "cut silence, reframe 9:16, burn captions" talk.mp4
# default renders and probes. --plan writes the program and stops.
# flags: --no-analyze  --revise N  --work-dir DIR
session/
  intent.json
  inventory.json
  decisions.md
  brief.yaml          # source of truth
  work/project.json
  work/render.sh
  rev1/               # critique may rewrite the brief once
# exit 0 planned or verified; exit 3 failed critique
```

`vedit tools` is that loop as stdio JSON for an external agent: `doctor`, `inventory`, `parse_intent`, `decide`, `plan`, `render`, `critique`, `revise`. The English parser is deterministic. The agent does not call an LLM. `report.json` is the proof. No paid caption or enhance APIs.

Open `Briefcut.app` to use the native editor window. Choose a clip, describe the
story, review the selected moments, and make the video. The result includes a
playable preview and a probe of the delivered file. Downloads open a native Save
dialog. Local mode needs no account; Meta mode is explicitly optional.

`vedit package dmg --out dist/Briefcut.dmg` stages `dist/Briefcut.app` and builds the
disk image. The bundle ID is `ai.aubric.briefcut`; minimum macOS is 15. Python
3.13+, FFmpeg, and an optional Whisper model must be installed separately. The
bundle does not vendor them. The native app keeps the existing
`~/Library/Application Support/Vedit` workspace for compatibility, or uses
`VEDIT_WORKSPACE` when set. `vedit studio` serves the same editor on loopback.

## What it does

Studio transfers each selected clip once per page visit, streaming it to disk in 256 KiB chunks. Refining the edit or rendering reuses that source; each request gets its own session folder. Interrupted transfers are removed and can be retried. Clips up to 200 MB work in Studio; use `vedit agent` with a local path for larger files. Sources and sessions stay under `vedit-sessions/` until you delete them.

For local clients, `POST /api/upload?name=clip.mp4` with an `application/octet-stream` body and `Content-Length` returns `{ "ok": true, "paths": ["..."], "bytes": 123 }`. Pass those `paths` to JSON `POST /api/run` with `goal`, `render`, and `analyze`. Existing multipart clients still work.

| Job | How |
|---|---|
| Cut / trim / concat / speed / reverse / freeze | FFmpeg trim, concat, setpts |
| Remove silence (jump-cut) | `silencedetect` → keep-ranges → `select`/`aselect` |
| Captions | local `whisper-cli` or `faster-whisper` → SRT/ASS → burn-in |
| Titles, watermarks, PIP | `drawtext` / `overlay` (ffmpeg-full) |
| Music duck under speech | `sidechaincompress` |
| Loudness | two-pass EBU R128 `loudnorm` |
| Color / denoise / sharpen / blur / chroma key | `eq`, `lut3d`, `hqdn3d`, `unsharp`, `boxblur`, `chromakey` |
| Aspect convert | fit (letterbox) or fill (center crop) to 16:9 / 9:16 / 1:1 / 4:5 |
| 3D-style footage effects | Curated perspective motion with bounded strength and timing; local `spatial_effect` op |
| Slideshow | stills + `xfade` |
| Transitions | `xfade` + `acrossfade` |
| Export | YouTube 1080p/4K, Shorts, preview, podcast audio, mezzanine |
| Hardware encode | auto `h264_videotoolbox` on Apple Silicon (never for `web_preview`); `--no-hw` forces libx264; VT failure retries software |
| Color | bt709 / tv range / yuv420p / `+faststart` / High profile on delivery presets |
| Interchange | `timeline.otio.json` (OTIO-lite) next to `project.json` |

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

## Execution

A YouTube brief is **two video encodes** (silence-cut, then one master with captions + fade + loudnorm + preset + broadcast tags), plus verify. Plan writes scripts only. `vedit render DIR` resumes a planned work dir. Encodes stream progress. Auto VideoToolbox on this Mac. No paid APIs.

Not a Premiere GUI. That is a different product.

## Install

Needs Python 3.13+ and FFmpeg. On this machine the right binary is `ffmpeg-full` (has `drawtext` + `subtitles`).

```bash
cd video_editor
python3.13 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
export VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg
vedit doctor
python scripts/make_demo.py   # tiny talk.mp4 / bed.wav / logo.png under examples/demo/
```

Optional captions:

```bash
brew install whisper-cpp
mkdir -p ~/.cache/whisper
curl -L -o ~/.cache/whisper/ggml-base.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin
export VEDIT_WHISPER_MODEL=$HOME/.cache/whisper/ggml-base.en.bin
```

## Tell it what to do

### One shot

```bash
vedit do talk.mp4 --cut-silence --captions --music bed.mp3 --preset youtube_1080p
vedit do "cut silence from talk.mp4; captions; duck bed.mp3; reframe 9:16; export shorts"
```

### Brief (source of truth)

```bash
vedit plan examples/youtube.yaml --work-dir /tmp/yt
vedit execute /tmp/yt                      # run the planned program; no recompile
vedit render /tmp/yt                       # same: a work dir with project.json is executable
vedit render --resume --work-dir /tmp/yt   # skip steps whose outputs already exist
vedit do talk.mp4 --cut-silence --plan --no-hw --work-dir /tmp/cut
```

### Recipe

```bash
vedit recipe list
vedit recipe apply podcast --in talk.mp4 --music bed.mp3
vedit recipe apply shorts --in talk.mp4
vedit recipe apply slideshow --images 01.png 02.png 03.png --music bed.mp3
```

### Python

```python
from vedit import Edit

Edit("talk.mp4").cut_silence().captions().music("bed.mp3").loudnorm().to("out/final.mp4")
```

## Brief language

```yaml
name: podcast-episode
canvas: { width: 1920, height: 1080, fps: 30, sample_rate: 48000 }
sources:
  talk: ./interview.mp4
  music: ./bed.mp3
ops:
  - { op: cut_silence, threshold_db: -34, min_silence: 0.5, pad: 0.14 }
  - { op: highpass, hz: 80 }
  - { op: captions, burn: true }
  - { op: music, source: music, duck: true, volume_db: -18 }
  - { op: loudnorm, i: -16 }
  - { op: export, presets: [podcast_video, podcast_audio] }
```

Ops: `trim`, `cut_silence`, `concat`, `speed`, `reverse`, `freeze`, `fit`, `fill`, `scale`, `crop`, `pad`, `rotate`, `hflip`, `vflip`, `fps`, `color`, `denoise`, `sharpen`, `blur`, `fade_in`, `fade_out`, `volume`, `highpass`, `lowpass`, `loudnorm`, `title`, `captions`, `overlay`, `watermark`, `pip`, `music`, `replace_audio`, `extract_audio`, `reframe`, `slideshow`, `thumbnail`, `chromakey`, `scenes`, `export`.

## Cost model

Local editing has zero API spend. FFmpeg, Whisper.cpp, and ImageMagick are already
on the box or free. Optional Meta features consume provider credits or paid usage
only when explicitly enabled, within the campaign cap. Sources, intermediate
renders, and archives use local disk; retain the accepted output before deleting
a completed campaign's work directory.

## What this is not

Not a Premiere/Resolve clone. A GUI timeline you click through is the wrong product if the job is “make the edit and give me a file.” vedit is the compiler those GUIs hide. If you want generated motion graphics (invented scenes, kinetic type), pair this with HyperFrames for titles and keep vedit as the NLE that cuts real footage.

## Tests

```bash
pytest
```
