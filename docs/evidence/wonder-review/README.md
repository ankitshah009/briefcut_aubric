# Muse Wonder vs Muse Spark as Briefcut's director

Live run on 2026-11-04, 18:33 to 18:35 UTC. One cut, two directors, the same request.

## The cut

- Source: `examples/demo/workshop.mp4`, a procedural clip (sha256 `ad6d10b2…1dac`).
- Cut: Briefcut campaign `cae7cc5b…`, render 1. It runs 18.9 s at 1080x1920, with boxed captions burned in.
- Goal: "Show how the oak bowl is made, start to finish".
- Both models got the same review proxy, built the way `CampaignManager._review_cut` builds it: `fit`, then `web_preview_9x16`. That is 540x960, 18.9 s, with AAC audio, 3.9 MB (sha256 `6feb09dd…8f31`).

## What each model received

- The proxy is uploaded once through the Files API with purpose `user_data`.
- It goes into the Responses API as an `input_file` block. The video part carries its own audio.
- The answer is strict JSON schema `vedit_review`, the same schema for both.
- Settings: `max_output_tokens` 16384, reasoning `medium`, `store` false.
- Spark gets Briefcut's usual prompt.
- Wonder gets the same prompt plus audio checks. It is told to listen, to compare the captions with the voice, and to trust its ears over the speech recognition.
- Spark ran as `muse-spark-1.3-contributor`, the project's setting.
- Wonder ran as `muse-wonder-1.0`. Model API has no contributor mode for Wonder, so it always runs in Standard mode.

## The reviews

| | Muse Wonder | Muse Spark |
| :-- | :-- | :-- |
| Model | `muse-wonder-1.0` | `muse-spark-1.3-contributor` |
| Hook score | 3 | 3 |
| Checks | 8 (6 pass, 2 fail) | 5 (5 pass) |
| Audio checks | 3 (all pass) | 0 |
| Patches | 2 | 0 |
| Input / output tokens | 5,401 / 3,968 | 6,579 / 5,897 |
| Cost | $0.1010 | $0.0018 |
| Reserved before the call | $0.3354 | $0.0035 |
| Meta response time | 36.9 s | 32.3 s |
| Whole call, upload to delete | 45.7 s | 46.4 s |
| Request id | `resp_6aeb7a5f9fd33a260c784d1a` | `resp_6aeb7a8dedea4453757245ed` |

Wonder failed two checks. Both are true. The frames in `frames-4.5s-8s-10s-13.5s.jpg` show it.

- Pacing: the same raw oak close-up holds from 3.3 s to 8.8 s across two sentences. The 4.5 s and 8 s frames show the same picture.
- Picture matches words: the sanding shot at 11.9 s looks like the lathe shot at 8.9 s. Both have the same spinning bowl and the same grey tool edge. The 10 s and 13.5 s frames are close to identical.

Spark passed both checks. For picture vs words it wrote "Saw spinning bowl with flying chips and white cutter", which is true of the lathe shot. It did not notice that the sanding shot repeats it.

Wonder's patches are workable edits. It sets `zoom.mode` to `slow` to give the long hold some motion. It sets sanding (moment 5) to 0.85x so the picture matches the spoken "turning slowly". Spark proposed no patches.

Wonder's three audio checks all passed:

- The captions match the voice.
- The voice is clear and even across the cuts.
- No cut leaves a pop, a jump in level, or a dead gap.

The audio facts below back this up.

## Can each model hear?

The review proxy has burned-in captions, so a model could read the words off the screen. To test hearing alone, both models got the same cut before captions were added. Nothing was written on screen, and no transcript was sent. Files: `hearing-wonder.json` and `hearing-spark.json`. Measured facts: `audio-facts.json`.

| | Muse Wonder | Muse Spark | What is true |
| :-- | :-- | :-- | :-- |
| Words | All six sentences, correct | All six sentences, correct | The six narrated sentences |
| Voice | "Medium-pitched, steady and slow-paced, flat and synthetic-sounding" | "male, medium-low pitch, slow steady pace, natural sounding" | macOS text-to-speech, median pitch 180 Hz |
| Other sounds | "none" | "quiet wood lathe humming with light scraping and sanding sounds" | None. Between sentences the track is digital silence. |
| Cost | $0.0331 | $0.0007 | |
| Request id | `resp_6aeb7aa37abb0b8216814571` | `resp_6aeb7ab54366f8913cb84628` | |

Both models got the words right. Only Wonder described the soundtrack truthfully. Spark invented a lathe sound that is not there. It also called a synthetic voice natural and male. For a director that judges audio, this is the difference that matters.

Meta's blog says audio use cases need "confirmation of audio support on the release surface". This run confirms that `muse-wonder-1.0` hears video audio on Model API.

## Spend

- Total for all four calls: $0.1366, against a $0.40 ceiling.
- Each call could spend only what the earlier calls left over.
- Wonder cost 55 times Spark in contributor mode.
- At Spark's Standard price, the same review would have cost about $0.033. Against that, Wonder cost about 3 times as much.
- Wonder also used fewer tokens for the same video. In the hearing probe, with the same prompt, it used 3,738 input tokens to Spark's 5,034.

## Budget behavior

- A Wonder review reserves its whole 16,384-token output allowance at $20 per million. That is $0.33 before the video is counted.
- Briefcut's default campaign budget is $0.25, so a Wonder review needs a higher cap. At that default Briefcut refuses with this message, and nothing is uploaded: "A Muse Wonder review can cost up to $0.3277 (its 16,384-token output allowance alone, before the video is counted, at $4 in and $20 out per million tokens, Standard mode only). The campaign budget is $0.2500. Raise the campaign budget to at least $0.33, or choose Muse Spark as the director."
- A budget of $0.40 or more covers one Wonder review of a short cut.

## One quirk, fixed after the run

- Wonder wrote the string "null" for `caption_suggestion` and for both patch reasons. The run's JSON shows it as returned.
- Briefcut now treats a bare "null" as no text, and the Wonder prompt now asks for real reasons.
- A test replays this answer.
- The new prompt was not re-run live. A second Wonder review can reserve up to $0.34, and only $0.26 of the ceiling was left.

## Limits of this proof

- One procedural clip, with one run per model.
- The voice is text-to-speech, and the clip has no music.
- A real clip with noise, music, or a caption that differs from the speech would test the audio checks harder.

## How to use it

- `meta.review_cut(..., director="wonder")`, or set `VEDIT_META_DIRECTOR=wonder`.
- The default stays Spark.
- Each review returns `model`, the exact id, and `director`, either `muse-spark` or `muse-wonder`.
