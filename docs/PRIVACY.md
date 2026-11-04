# Privacy

Briefcut runs on your computer and uses Meta's models through the Meta Model API. This page says what is sent to Meta for each feature, when it is sent, and what never leaves your computer. It was written by reading the code in this repository.

## What stays on your computer

- **Your original files.** Briefcut sends Meta a smaller copy, an audio track, a still, or text, as the table below says. The original clip never goes to Meta.
- **Rendering.** FFmpeg renders every version on this computer. Cutting, captions burn-in, color, effects, loudness, and export all happen here.
- **Your projects.** Plans, edits, every render, the stored copy of each clip, the transcript Meta returns, and cached Meta results stay in the project folder shown in Settings.
- **Receipts and the ledger.** `report.json` (the probe of each delivered file and its checks) and `meta-usage.json` (every Meta reservation and its usage) are local files.
- **Settings and your key.** Settings live in `~/.vedit/config.json`, readable only by your account. Your Meta API key goes in the macOS Keychain when one is available, and otherwise in that file. A key in `.env.local` or the environment is read from there and never written anywhere else.

The Studio page talks only to a server on `127.0.0.1` that runs on your own machine. It loads nothing from the internet: no web fonts, no scripts from another site.

Deleting a project in Projects removes it from disk. It does not touch your original clip.

## No telemetry

Briefcut sends no analytics, no crash reports, and no usage data. There is no Briefcut account. Nothing runs in the background to phone home, and the app does not check for updates.

"Copy diagnostics" puts text on your clipboard for you to paste yourself. It sends nothing, and it leaves out home folder paths, keys, and transcript text.

## When anything goes to Meta

Nothing is sent by opening Studio or by having a key saved. A Meta call needs all three of these:

1. a key (in Settings, `MODEL_API_KEY`, or `.env.local`),
2. "Allow Meta calls" switched on in Settings, or `VEDIT_META_ENABLED=1`,
3. a project you start, after you tick the box that confirms this footage may be sent to Meta.

<!-- VERIFY: in Meta-only mode, confirm the consent box is still required before Create story, and whether the VEDIT_META_ENABLED switch remains. -->

Coding agents follow the same rule. Over MCP, `briefcut_edit` needs `allow_meta` before a change sends captions or footage, and `briefcut_generate_background` needs `consent`.

Studio's Meta calls panel lists each call as it happens: the model id, what it was for, how long it took, its cost, and Meta's request id. <!-- VERIFY: panel name and location; built and browser-checked, not yet seen on a live Meta run. -->

Each project has a spending cap ($0.25 by default, $5 at most). Briefcut reserves the worst-case cost before each call and stops before the cap.

## What is sent to Meta, by feature

| What you do | Model | What is sent |
| --- | --- | --- |
| Create a story, or make shorts from one video | Meta Voice Transcribe, then Muse Spark | A mono 16 kHz audio file of the project for the transcript. Then a 960×540 copy of your whole project, your brief, and that transcript. |
| Find a moment | Muse Spark | The same 960×540 copy and your search words. Searching what was said runs on this computer, over the transcript. |
| Keep a subject in frame | SAM 3.1 | A 960×540 copy of the moments you selected, and your description of the subject. |
| Cut a subject out of its background | SAM 3.1 | The same kind of copy, and your description of the subject. |
| Captions | Meta Voice Transcribe | A mono 16 kHz audio file of the moments you selected. |
| Translated captions | Muse Spark | The caption text, with no audio or video. |
| Generate a cover | Muse Image | One still frame from the finished video, and the cover instructions. |
| Generate a background | Muse Image | Your description of it, with no footage. |
| Turn a written script into a video | Muse Spark, then Muse Image | The text of your scenes to Muse Spark. The picture prompts it writes go to Muse Image. No footage. |
| Ask Meta to review the finished cut | Muse Spark | A 540p copy of the finished video, your brief, and the story plan. |
| A director's note | Muse Spark | Your note, your brief, the story plan, and its sentences, as text. |
| Test connection | none | Your key, to list the models your account can use. No media and no inference. |

<!-- VERIFY: (1) the planning transcript call: is it the whole project's audio, at what sample rate, and how is a source over 10 minutes handled? (2) Script-to-video narration: what makes the voice now that macOS `say` is removed, and is anything sent for it? (3) Whether find still searches the transcript locally. -->

Uploaded files carry a one-hour expiry, and Briefcut deletes each one after the operation. If a deletion fails, the project keeps a `meta-cleanup.json` file with the remote file's id and a warning appears, so you can finish it yourself. Files are uploaded under a generic name, not your file name. Your key goes only to `api.meta.ai`, and redirects are refused so it cannot be forwarded. Successful results are cached in the project, so repeating the same request does not send it again.

Meta's own terms cover what Meta does with what it receives.

### Contributor mode

Contributor mode is off unless you switch it on in Settings or set `VEDIT_META_MODE=contributor`. It makes Muse Spark cheaper ($0.10 and $0.20 per million tokens instead of $1.25 and $4.25), and in exchange Meta may use what you send to train its future models. That covers every Muse Spark request above: stories, shorts, finding moments, translation, script scenes, reviews, and notes. Those requests use the model `muse-spark-1.3-contributor`.

SAM 3.1, Muse Image, and Voice Transcribe have no contributor mode. They keep their normal prices and terms in either mode.

Leave contributor mode off for anything you would not hand to a third party for training, such as client footage or footage of people who have not agreed. Every Muse Spark call recorded in `docs/evidence/` used contributor mode, on generated demo footage only.

### Private mode

<!-- VERIFY: placeholder. If a private mode ships (an open-weight Meta model such as Muse Glimmer or Muse Spark Open, run on this computer through llama.cpp, for text-only work like director's notes and translation), describe here exactly which requests then stay on the computer. Delete this section if it does not ship. -->

Not available in this build. Every model call above goes to the Meta Model API.

## The other connections

Each of these happens only when you ask for it.

- **Installing.** `scripts/install.sh` contacts Homebrew or apt, `astral.sh` for uv, and PyPI for dependencies. Those projects have their own policies.
- **Claude Code plugin.** Its first start installs Briefcut's Python packages from PyPI into the plugin's folder.

## Getting everything off your computer

Delete the project folder shown in Settings, delete `~/.vedit`, and, on a Mac, remove "Briefcut Meta API key" in Keychain Access. To remove files Meta still holds after a failed deletion, use the ids in any `meta-cleanup.json`.
