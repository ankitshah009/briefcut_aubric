# Privacy

Briefcut runs on your computer and uses Meta's models. Most calls go to the Meta Model API. In private mode, three text-only steps run on Muse Glimmer on your own computer instead. This page says what is sent to Meta for each feature, when it is sent, and what never leaves your computer. It was written by reading the code in this repository.

## What stays on your computer

- **Your original files.** Briefcut sends Meta a smaller copy, an audio track, a still, or text, as the table below says. The original clip never goes to Meta.
- **Rendering.** FFmpeg renders every version on this computer. Cutting, burning in captions, color, effects, loudness, and export all happen here. Blender, when installed, renders 3D motion cards here too.
- **Your projects.** Plans, edits, every render, the stored copy of each clip, the transcript Meta returns, and cached Meta results stay in the project folder shown in Settings.
- **Receipts and the logs.** `report.json` (the probe of each delivered file and its checks), `meta-calls.jsonl` (the log Studio's Meta calls panel reads), and `meta-usage.json` (every reservation and its usage) are local files.
- **Settings and your key.** Settings live in `~/.vedit/config.json`, readable only by your account. Your Meta API key goes in the macOS Keychain when one is available, and otherwise in that file. A key in `.env.local` or the environment is read from there and never written anywhere else.

The Studio page talks only to a server on `127.0.0.1` that runs on your own machine. It loads nothing from the internet: no web fonts, no scripts from another site.

Deleting a project in Projects removes it from disk. It does not touch your original clip.

## No telemetry

Briefcut sends no analytics, no crash reports, and no usage data. There is no Briefcut account. Nothing runs in the background to phone home, and the app does not check for updates.

"Copy diagnostics" puts text on your clipboard for you to paste yourself. It sends nothing, and it leaves out home folder paths, keys, and transcript text.

## When anything goes to Meta

Nothing is sent by opening Studio or by having a key saved. A Meta call needs all three of these:

1. a key (in Settings, `MODEL_API_KEY`, or `.env.local`),
2. "Allow Meta calls" switched on in Settings, or `VEDIT_META_ENABLED=1`. Switching it off pauses every Meta call,
3. a story you create. Studio asks you to tick "Send this footage to Meta using my API key" first, and refuses to create the story without it.

Consent is given once, when the project is created. Later steps on that project, such as captions, review, find, or a cutout, use the same consent and the same spending cap. Coding agents follow the same rule: creating a story over MCP sends the footage, `briefcut_edit` reports in its `meta` field when a change would send more at render (a new caption language, or a cutout), and `briefcut_generate_background` still needs `consent: true`.

Studio's Meta calls panel lists every call the open project makes, live: what it was for, its status, the model, its cost, how long it took, its size, and Meta's response id. It reads the project's `meta-calls.jsonl`, which never holds keys, prompts, transcripts, or footage.

Each project has a spending cap ($0.25 by default, $5 at most). Briefcut reserves an upper bound before each call, including an allowance for video that Meta's token count leaves out, and stops before the cap.

## What is sent to Meta, by feature

| What you do | Model | What is sent |
| --- | --- | --- |
| Create a story | Meta Voice Transcribe, then Muse Spark | The project's speech as mono 16 kHz audio, with silence left out. Then a 960×540 copy (540×960 for upright footage) of your whole project, your brief, and the transcript Meta returned. Up to 10 minutes of footage. |
| Make shorts from one video | Muse Spark | The same kind of copy, your brief, and the transcript, which the shorts reuse. |
| Captions | Meta Voice Transcribe | Usually nothing more: captions are cut from the transcript Meta made when you created the story. If that failed, the speech of the moments you kept, as mono 16 kHz audio. |
| Punch words in captions | Muse Spark | The caption text, your brief, and the language. No audio or video. |
| Translated captions | Muse Spark, or Muse Glimmer in private mode | The caption text, with no audio or video. In private mode it stays on this computer. |
| Find a moment | Muse Spark, then SAM 3.1 | The project copy and your search words to Muse Spark. Then, to check what it found, up to 5 short windows of the footage (3 s each, at most 15 frames a second, 540 px tall) and a short name of the thing to SAM 3.1. Searching what was said runs on this computer, over the transcript, and sends nothing. |
| Keep a subject in frame | SAM 3.1 | A copy of the moments you kept, and your description of the subject. |
| Cut a subject out of its background | SAM 3.1 | The same kind of copy, and your description of the subject. |
| Hero word | Muse Spark, then SAM 3.1 | The cut's caption words to Muse Spark, with no media. Then a copy of the 0.5 to 3 s window the word appears in, and your description of the subject, to SAM 3.1. |
| 3D motion card | Muse Spark, and Muse Image for the backdrop | Your brief and the cut's caption words to Muse Spark. Its one-sentence backdrop description to Muse Image. No media. Blender renders the card here. |
| Generate a cover | Muse Image | One still frame from the finished video, and the cover instructions. |
| Generate a background | Muse Image | Your description of it, with no footage. |
| Turn a written script into a video | Muse Spark (or Muse Glimmer in private mode), then Muse Image | The text of your scenes. The picture prompts it writes go to Muse Image. No footage. Nothing is sent for a voice: the video is caption-led, because Meta has no text-to-speech model. |
| Ask for a review of the finished cut | Muse Spark, or Muse Wonder if you choose it | A 540p copy of the finished video with its sound, your brief, and the story plan. Muse Wonder is asked to listen to the sound as well. |
| A director's note | Muse Spark, or Muse Glimmer in private mode | Your note, your brief, the story plan, and its sentences, as text. In private mode it stays on this computer. |
| Test connection | none | Your key, to list the models your account can use. No media and no inference. |

Uploaded files carry a one-hour expiry, and Briefcut deletes each one after the operation. If a deletion fails, the project keeps a `meta-cleanup.json` file with the remote file's id and a warning appears, so you can finish it yourself. Files are uploaded under a generic name, not your file name. Your key goes only to `api.meta.ai`, and redirects are refused so it cannot be forwarded. Successful results are cached in the project, so repeating the same request does not send it again.

Meta's own terms cover what Meta does with what it receives.

### Contributor mode

Contributor mode is off unless you switch it on in Settings or set `VEDIT_META_MODE=contributor`. It makes Muse Spark cheaper ($0.10 and $0.20 per million tokens instead of $1.25 and $4.25), and in exchange Meta may use what you send to train its future models. That covers every Muse Spark request above: stories, shorts, finding moments, punch words, the hero word, motion cards, translation, script scenes, reviews, and notes. Those requests use the model `muse-spark-1.3-contributor`.

Muse Wonder, SAM 3.1, Muse Image, and Voice Transcribe have no contributor mode. They keep their normal prices and terms in either mode. Muse Glimmer in private mode sends nothing to Meta, so contributor mode does not apply to it.

Leave contributor mode off for anything you would not hand to a third party for training, such as client footage or footage of people who have not agreed. Every Muse Spark call recorded in `docs/evidence/` used contributor mode, on demo clips and public samples only.

### Private mode

Private mode runs three text-only steps on Muse Glimmer 30B (`meta-models/Muse-Glimmer-30B`), Meta's open-weight model, on this computer through llama.cpp: the director's note, caption translation, and the script scene writer. For those steps, no text leaves the computer, no Meta key is used, and nothing is billed.

Turn it on with **Private mode** in Settings, or `VEDIT_PRIVATE_MODEL=glimmer`. Start the local server first with `uv run python -m vedit.open_weights serve`. The server listens only on `127.0.0.1`, needs a key from a 0600 file, and has its web UI and slot monitor off.

Private mode fails closed. If the server is down, serves a different model, or is not on this machine, the step stops with an error. It never falls back to the Meta API.

Private mode does not cover story planning from video, the review of the finished cut, SAM 3.1, Muse Image, or Voice Transcribe. Those still go to the Meta Model API as the table says. Each local call is logged in the project's `open-weights-calls.jsonl` and `meta-calls.jsonl`, and the Meta calls panel tags it "Private". The [evidence](evidence/open-weights/README.md) shows a run with the Meta API call replaced by a function that would fail the run.

## The other connections

Each of these happens only when you ask for it.

- **Installing.** `scripts/install.sh` contacts Homebrew or apt, `astral.sh` for uv, and PyPI for dependencies. Those projects have their own policies.
- **Muse Glimmer weights.** You download them yourself from Hugging Face (`meta-models/Muse-Glimmer-30B-GGUF`). Briefcut does not download them for you.
- **Claude Code plugin.** Its first start installs Briefcut's Python packages from PyPI into the plugin's folder.

## Getting everything off your computer

Delete the project folder shown in Settings, delete `~/.vedit`, and, on a Mac, remove "Briefcut Meta API key" in Keychain Access. `~/.vedit` also holds cached 3D motion cards (`~/.vedit/cards`) and any Muse Glimmer weights you downloaded (`~/.vedit/models/open-weights`). To remove files Meta still holds after a failed deletion, use the ids in any `meta-cleanup.json`.
