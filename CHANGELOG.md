# Changelog

## Unreleased

- Studio transfers each selected clip once and reuses it when the edit changes or a render starts. Uploads stream to disk with bounded memory, a timeout, and cleanup of incomplete transfers.
- Automatically allocated session folders are unique even for concurrent requests with the same goal and timestamp. Explicit work directories keep their existing behavior.
- Outdated plan responses are ignored as soon as the edit text changes; failed transfers can be retried without reloading the editor.

## 1.2.0

- `vedit agent "goal" path.mp4` writes a session: `intent.json`, `inventory.json`, `decisions.md`, `brief.yaml`, `work/project.json`, `work/render.sh`. Render is the default and probes the output. `--plan` stops before execute. Optional flags: `--no-analyze`, `--revise N`, `--work-dir DIR`.
- Critique may revise the brief once into `rev1/`. Exit 0 means planned or verified. Exit 3 means critique failed.
- `vedit tools` is a stdio JSON protocol: `doctor`, `inventory`, `parse_intent`, `decide`, `plan`, `render`, `critique`, `revise`.
- `Vedit.app` opens the editor in its own window and does not open the system browser. `vedit studio` still serves that editor on 127.0.0.1. No account, no cloud. App Sandbox stays off so the window can launch the machine's Python and ffmpeg-full.
- The window shows pauses, shape, captions, and the program as soon as the sentence and the clip are both set. Make the video then plays the probed file.
- Make the video returns when the file is probed. It does not wait on a VMAF pass. Set `VEDIT_VMAF=1` to opt in.
- If the window blocks sound on the first play, the cut still starts muted and says so. Unmute on the player when you want the sound.
- `vedit package dmg --out dist/Vedit.dmg` stages `dist/Vedit.app` and builds a UDZO disk image. The app needs Python 3.13+ and ffmpeg already on the machine. Bundle id `com.ankitshah.vedit`.
- The agent does not call an LLM. The English parser is deterministic. No paid caption or enhance APIs. The brief remains the source of truth.

## 1.1.1

- Plan no longer writes empty `captions.srt` / `captions.ass`. `vedit render DIR` was skipping transcription because those placeholders already existed.
- Resume requires a non-empty output file, not just a path that exists.
- `resolve_font()` finds DejaVu / Liberation / Noto on Linux; CI installs `fonts-dejavu-core`.
- Stream ffmpeg stderr only when stderr is a TTY (pytest capture was deadlocking).

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
