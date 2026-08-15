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
| Hardware encode | optional `h264_videotoolbox` on Apple Silicon |

Every run writes:

```text
out/<name>/
  brief.yaml      # what you asked for
  project.json    # compiled step graph
  render.sh       # standalone, re-runnable
  steps/*.sh      # one script per step
  report.json     # durations, sizes, streams
  *.mp4 / *.mp3   # outputs
```

## Score

**8.2 / 10** as an agent-native local NLE compiler. Not Premiere. Not DaVinci.

| Dimension | Score | Why it is not 10 |
|---|---|---|
| Intent → inspectable program | 9 | Brief + `render.sh` is the product |
| Zero API cost | 10 | FFmpeg / Whisper.cpp / ImageMagick only |
| Plan without decoding | 8.5 | Silence, captions, loudnorm are render steps |
| Plan reuse | 8 | `vedit execute DIR` / `render --resume` |
| Edit quality | 7.5 | Multi-encode tape, not a single filter graph |
| Coverage | 8 | Real NLE ops; no GUI, no OTIO export file yet |
| Daily-driver proof | 8 | 25+ tests + demo assets + doctor |

World-class here means: you tell it the edit, you get a rebuildable program, it renders locally, and a failed step is never a mystery.

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
vedit execute /tmp/yt                 # run the planned program; no recompile
vedit render --resume --work-dir /tmp/yt   # skip steps whose outputs already exist
vedit do talk.mp4 --cut-silence --plan --work-dir /tmp/cut
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
