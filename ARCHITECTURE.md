# Architecture

## Product

One magical job: describe an edit in English, YAML, or Python; get a **re-runnable program** and a **verified file**.

Latency: probe and plan are immediate; render streams step-by-step. The plan is written to disk before the first encode, so a failed render is never a black box. Silence-cut and Whisper are `python -m vedit.analyze` / `vedit.captions` pipeline steps — `vedit plan` does not decode the timeline.

What power users hate: opaque filter graphs, cloud lock-in, projects that cannot be rebuilt, “AI editors” that will not re-run. vedit’s source of truth is `brief.yaml` + `render.sh`.

Cost per use: $0. Failure UX: `vedit doctor` names the missing binary; each step dumps the exact argv.

10/10 output: 16:9/9:16 canvases, yuv420p, +faststart, two-pass loudnorm, SRT sidecar plus burn-in, report.json with probed duration/streams.

## Why not a GUI NLE

Shotcut, Kdenlive, OpenShot, Flowblade, Olive, and Blender VSE are real editors. They all sit on FFmpeg or MLT. Their interchange problem is why OpenTimelineIO exists. Their automation problem is why editly and auto-editor exist.

The missing product — the one you can tell what to do — is not another Qt timeline. It is a **compiler**:

1. Brief (human / agent writable)
2. Analysis (silence, speech, loudness, scenes)
3. Tape + filter accumulator (minimize re-encodes)
4. Pipeline of named FFmpeg steps
5. Codegen (`render.sh`) so the computer is not required to re-invent the command
6. Probe the output or fail loudly

## Data flow

```text
Brief.ops[] ──► Compiler ──► Pipeline.steps[] ──► execute()
                    │                 │
                    │                 ├── project.json
                    │                 └── render.sh
                    ▼
              Tape (current file + pending vf/af)
                    │
                    ├── flush on analysis / new input / export
                    └── encode intermediates veryfast CRF 16
```

Ops that only change filters (`color`, `fit`, `fade_in`, `speed`, …) accumulate. Ops that need a real file (`cut_silence`, `captions`, `music`, `concat`, `loudnorm`, `export`) materialize first.

## IR

`Brief` is the user-facing IR (editly-like, but multi-op and analysis-aware).  
`Canvas` is the sequence settings (MLT profile).  
`Clip` / `Track` exist for a future tractor compiler; the tape compiler covers the jobs that pay the rent today.

Timestamps are seconds (float). Export is OTIO-shaped JSON in `project.json` so a later OTIO writer is a mapping, not a rewrite.

## Engines

| Engine | Role | Required |
|---|---|---|
| `ffmpeg-full` | render, filters, encode | yes |
| `ffprobe` | MediaInfo | yes |
| `whisper-cli` + ggml | captions | optional |
| `faster-whisper` | captions fallback | optional |
| ImageMagick | future title raster fallback | optional |
| VideoToolbox | `--hw` encode | optional |

`VEDIT_FFMPEG` overrides the binary. Default prefers `/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg` because the stock Homebrew `ffmpeg` bottle on this Mac has no `drawtext`/`subtitles`.

## Failure modes

- Missing ffmpeg → `BinaryMissing`, doctor points at brew.
- Empty keep-ranges → `AnalysisError` (file is all silence).
- Whisper missing → `CaptionError` with the one-time model curl.
- Step non-zero → `RenderError` with argv + last 8 stderr lines.
- Output missing after “success” → treated as failure.

## What we refused

- MoviePy as the engine (slow, hides the graph, extra dep).
- One mega `filter_complex` for every job (un-debuggable).
- Paid STT / paid render / paid stock.
- Shipping a thin “cut two files together” demo and calling it an editor.
