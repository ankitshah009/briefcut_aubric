# Changelog

## 1.1.0

- Auto VideoToolbox on Darwin for delivery presets; `web_preview` stays libx264. `--hw` / `--no-hw` / `hw: auto`.
- Hardware encode falls back to libx264 if VideoToolbox fails.
- YouTube / Shorts / mezzanine presets tag bt709 + tv range + high profile + faststart.
- `vedit render DIR` resumes a planned `project.json` work directory.
- Every plan writes `timeline.otio.json` (OTIO-lite interchange).
- Execute prints `[n/total]` and streams ffmpeg stderr so long encodes are not a black box.
- Master encode and silence-cut write `.part` then rename (crash-safe).
- Verify reports color tags. Probe exposes primaries / trc / space / range.
- Spoken-caption proof against whisper-cpp `jfk.wav` (skipped when the fixture is absent).
- GitHub Actions CI on Python 3.13 + apt ffmpeg.

## 1.0.0

- Agent-native local NLE compiler: brief → project.json / render.sh → FFmpeg.
- Deferred silence-cut, captions, two-pass loudnorm. YouTube path is two encodes + verify.
