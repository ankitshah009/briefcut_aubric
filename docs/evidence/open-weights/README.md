# Private mode: Muse Glimmer on this Mac

Briefcut's private mode moves the text-only Muse Spark work onto Meta's open-weight model, Muse Glimmer. It runs on the editor's own Mac through llama.cpp. In private mode the director's note, caption translation and the script scene writer never leave the machine. No Meta key is needed and the calls cost $0.

Recorded 2026-11-04 on an Apple M5 Max with 128 GB of memory, macOS (Darwin 27.0.0).

## The model and where it runs

| | |
| --- | --- |
| Model | Muse Glimmer 30B, `meta-models/Muse-Glimmer-30B`. Meta's open-weight model, distilled from Muse Spark. |
| License | Apache 2.0 (`LICENSE` and `USAGE_POLICY.md` from the weights repo are kept next to the weights). |
| Weights | Official GGUF from `meta-models/Muse-Glimmer-30B-GGUF`: `Muse-Glimmer-30B-KQuant-Dynamic-Q4_K_XL.gguf`, 19,653,960,832 bytes. This is the larger of the two official text builds. |
| sha256 | `ac7023d6a4c704eb9af54ab53e476a66b7f5b6c0ef2fc4a8dde5253c291a6c38`. It matches the Hugging Face LFS record (`sha256-verify.txt`). |
| Drafter | Official DFlash drafter `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` for speculative decoding, sha256 `b2e808bf...b264ef`, also verified. |
| Runtime | llama.cpp `llama-server` version 0.7.0, build 11669, commit 620d2225c, Homebrew, Metal. Every layer is on the GPU. |
| Address | `127.0.0.1:8091` only. The server requires a key from a 0600 file, and the slot monitor and web UI are off (`server-hardening.txt`). |
| Memory | 22 to 23 GB resident for the server, with two 65,536-token slots. |
| GGUF header | `general.architecture = muse-glimmer`, 52 blocks, 131,072-token context (`gguf-metadata.txt`). |

The server was started by Briefcut's own launcher:

```
uv run python -m vedit.open_weights serve --draft
```

which runs:

```
llama-server -m ~/.vedit/models/open-weights/Muse-Glimmer-30B-KQuant-Dynamic-Q4_K_XL.gguf \
  -a muse-glimmer-30b -ngl 99 -c 131072 -np 2 --host 127.0.0.1 --port 8091 --jinja \
  --temp 1.0 --top-p 0.95 --top-k 64 --no-slots --no-webui \
  --api-key-file ~/.vedit/models/open-weights/llama-server.key \
  -md ~/.vedit/models/open-weights/dflash-Muse-Glimmer-30B-Q4_K_M.gguf -ngld 99
```

The server starts with the model card's sampling (temperature 1.0, top_p 0.95, top_k 64). Briefcut's requests set temperature 0.3 for structured edits and keep the model card's top_p and top_k (see the temperature section below). Reasoning strength is medium. `llama-server-startup.log` shows the model file loading and the server listening.

## What it did

`run_proof.py` calls the same `vedit.meta` functions that Studio calls. For the whole run, `meta._request` is replaced by a function that fails the run, and no Meta key is set. Any attempt to reach the Meta API would have stopped it.

Inputs (`inputs.json`):

- Two approved story plans and the director's notes typed against them, from `docs/evidence/20261031T083945Z.json` and `docs/evidence/20261031T100101Z-note.json`. Muse Spark on the Meta API handled both notes on 2026-10-31.
- The ten spoken sentences of `examples/demo/workshop.mp4`, transcribed on this Mac with whisper-cli.

Run 4 is the final run at the shipped settings (`outputs-run4.json`, `open-weights-calls-run4.jsonl`): hardened server, temperature 0.3, and the second try for incomplete patches.

| Call | Input | What Muse Glimmer returned | Time | Tokens in / out | Decode |
| --- | --- | --- | --- | --- | --- |
| `patch_plan` | "Hold the finished bowl a beat longer before the first cut, then tighten the sanding moment." | Moment 1 now ends at 5.0 s instead of 4.585 s. The sanding moment now ends at 28.0 s instead of 29.0 s. Both applied. | 95.4 s | 1,553 / 1,747 | 18.6 tok/s |
| `patch_plan` | "warmer, bigger captions, slow down the oil moment" | Look set to Warm, captions to 1.25x, moment 6 (oil finish) to 0.8x. All three applied. | 57.9 s | 1,603 / 1,201 | 21.0 tok/s |
| `translate_cues` | 10 caption lines, English to Spanish | 10 lines, same timings. | 31.3 s | 326 / 772 | 25.2 tok/s |
| `translate_cues` | 10 caption lines, English to Hindi | 10 lines, same timings. | 38.8 s | 326 / 840 | 22.0 tok/s |
| `script_prompts` | 3 script scenes, 9:16 | One shared style and one picture prompt per scene. | 49.8 s | 274 / 914 | 18.9 tok/s |

Run 4 shared the GPU with another set of test calls, so its decode speeds are lower than those of run 2 (25 to 32 tok/s, same calls, alone on the server).

Every answer was schema-constrained JSON: llama.cpp built a grammar from the same JSON schema the Meta API call uses. Briefcut validated every answer and ran the patches through its deterministic patcher (`applied` in the outputs). The model's reasoning stays in `reasoning_content`, separate from the answer, and the output token counts include it. All five calls used the GGUF whose sha256 matches the official file.

Earlier runs with the same inputs:

- Run 1 (`outputs-run1.json`, `llama-server-run1.log`): before the server was hardened, temperature 1.0. 31 to 102 s per call. The hold note became two speed changes (moment 1 at 0.85x, moment 4 at 1.25x).
- Run 2 (`outputs-run2.json`): hardened server, temperature 1.0. 24 to 48 s per call. The hold note's first patch used `trim_start` where it needed `trim_end`, so only the sanding trim applied.
- Run 3 (`outputs-run3.json`): temperature 0.3, before the second try existed. The hold note's two patches came back without their times and were dropped. That result is what led to the second try.

## Compared with Muse Spark on the Meta API, same inputs

- "warmer, bigger captions, slow down the oil moment". Muse Spark: Warm look, captions 1.2x, oil moment 0.8x. Muse Glimmer in run 4: Warm look, captions 1.25x, oil moment 0.8x. Other runs chose captions 1.3x to 1.5x and the oil moment at 0.8x or 0.85x.
- "Hold the finished bowl a beat longer, then tighten the sanding moment". Muse Spark: moment 1 ends at 5.58 s instead of 4.585 s, and the sanding moment ends at 28.0 s. Muse Glimmer in run 4: moment 1 ends at 5.0 s, the end of the first spoken sentence, and the sanding moment ends at 28.0 s. Its answers to this note vary more from run to run. See the next section.
- Spanish captions. Muse Glimmer's first line, "Este es el cuenco de roble terminado, aceitado y listo para la mesa.", matches word for word the line Muse Spark wrote for the same clip in `docs/evidence/20261031-live-translation.json`.

## Run-to-run variation on the hard note

The two-part note "Hold the finished bowl a beat longer before the first cut, then tighten the sanding moment" is the hardest input. Every result below went through the real `meta.patch_plan` and Briefcut's patcher. A run passes when the opening moment plays longer and the sanding moment gets shorter.

| Temperature | Hold note: both requests | Warmer note: all three edits |
| --- | --- | --- |
| 0.3 | 6 of 8 | 3 of 3 |
| 1.0 (model card) | 4 of 8 | 3 of 3 |

At 0.3, the two misses returned no usable patch, so Studio would report that nothing could be applied. At 1.0, the misses dropped the "hold" request or returned nothing. Raw results are in `temperature-ab-pilot.txt` (first four runs per setting) and `temperature-ab.jsonl` (the rest).

Private mode therefore uses temperature 0.3 for these structured edits, with the model card's top_p 0.95 and top_k 64. `VEDIT_PRIVATE_TEMPERATURE=1.0` restores the model card value. Runs 1 and 2 used 1.0. Runs 3 and 4 use the 0.3 default. After the second try was added, three more runs of the hold note at 0.3 (`retry-check.jsonl`) gave two trims like run 4 and one reading that slows the opening moment instead of extending it. All three applied cleanly. None needed the second try. These are small samples on two notes, not a benchmark.

## In Studio

`studio-meta-calls-light.png` and `studio-meta-calls-dark.png` show Studio's Meta calls panel for a real project. Five calls went to the Meta Model API on 2026-10-31: story plan, SAM 3.1 tracking, Voice Transcribe, review and cover. Two calls ran here on 2026-11-04 in private mode: the director's note "warmer, bigger captions, slow down the oil moment" and a Spanish translation of its ten caption lines. The local rows carry a "Private" tag that reads "Muse Glimmer 30B · on this Mac (llama.cpp b11669)" and cost $0.00. The footer says "5 calls on the Meta Model API, 2 on this Mac with Muse Glimmer." `studio-meta-calls-running.png` was taken while the translation was still running. The Glimmer note produced Warm look, captions 1.5x and the oil moment at 0.85x. Those records are in `open-weights-calls-studio.jsonl` and `meta-calls-studio-local.jsonl`.

## Every call is on the record

Each local call writes three records in the job's `meta` folder, beside the Meta API calls (see `work/`):

- `open-weights-calls.jsonl`: the model id, GGUF file name and sha256, whether the hash matches the official one, the llama.cpp build, reasoning strength, sampling, tokens, latency, llama.cpp timings (including drafter acceptance), and the first 600 characters of the answer.
- `meta-calls.jsonl`: the shared call log that Studio's Meta calls panel reads. Local rows carry `"runs_on": "local"`, the runtime (`llama.cpp b11669-620d2225c`) and the weights file name, and cost $0.
- `meta-usage.json`: the project budget ledger, with $0 reserved and $0 spent for local calls.

`llama-server-run1.log` is the server's log for run 1. `llama-server-run2.log` covers the hardened server: runs 2 to 4, the temperature runs and the Studio calls. Their slot lines show prompt and decode speed and drafter acceptance. Prompts are not logged.

## Reproduce

```
# once: about 21 GB
mkdir -p ~/.vedit/models/open-weights && cd ~/.vedit/models/open-weights
curl -L -C - -O https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF/resolve/main/Muse-Glimmer-30B-KQuant-Dynamic-Q4_K_XL.gguf
curl -L -C - -O https://huggingface.co/meta-models/Muse-Glimmer-30B-GGUF/resolve/main/dflash-Muse-Glimmer-30B-Q4_K_M.gguf

# terminal 1
uv run python -m vedit.open_weights serve --draft

# terminal 2
uv run python -m vedit.open_weights status
VEDIT_PRIVATE_MODEL=glimmer uv run python docs/evidence/open-weights/run_proof.py run3
```

Private mode is off unless `VEDIT_PRIVATE_MODEL=glimmer` is set (or `"private_model": "glimmer"` in `~/.vedit/config.json`). It fails closed. If the server is down, serves another model, or is not on 127.0.0.1, the call raises an error and nothing goes to the Meta API.

## Limits

- Only text-only calls run locally: the director's note, caption translation and the script scene writer. Story planning from video, the review of the finished cut, SAM 3.1 tracking, Muse Image and Voice Transcribe still use the Meta Model API.
- The weights are a 4-bit quantization. Meta's model card reports minimal degradation on agentic tasks. No bf16 GGUF is published.
- Grammar-constrained decoding runs at 21 to 32 tok/s here. Unconstrained decoding with the drafter reached 54 tok/s. A note takes 40 to 100 s locally.
- Muse Glimmer sometimes names the right edit but leaves out its number, for example a `trim_end` with no time. Briefcut's validator drops such a patch, and private mode then asks Glimmer once more, saying which patch was incomplete. Both calls are logged. The second answer is used only if it applies more patches. Briefcut never applies a malformed patch, so the worst case is a smaller edit or a clear "nothing could be applied" message.
