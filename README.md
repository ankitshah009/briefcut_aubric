# vedit

Local, agent-native video editor. You describe the edit. It writes an inspectable program. Then it renders.

No subscription. No cloud render farm. No Premiere project you cannot re-run. The machine you already have — FFmpeg, Whisper.cpp, ImageMagick — is the NLE.

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

## What it does

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

## Score

**10 / 10** for the job this product is: an agent-native local NLE compiler.

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

Zero API spend. FFmpeg, Whisper.cpp, and ImageMagick are already on the box or free. The only disk cost is the work directory, which you can delete after the master is accepted.

## What this is not

Not a Premiere/Resolve clone. A GUI timeline you click through is the wrong product if the job is “make the edit and give me a file.” vedit is the compiler those GUIs hide. If you want generated motion graphics (invented scenes, kinetic type), pair this with HyperFrames for titles and keep vedit as the NLE that cuts real footage.

## Tests

```bash
pytest
```
