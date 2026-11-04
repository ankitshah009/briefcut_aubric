# Why Briefcut

## The problem

Video editors come in two kinds today. A manual timeline gives you full control and costs hours. An AI editor is fast, but it is a black box. You cannot see why it cut where it did. You cannot script it. You cannot check that the file it hands back is what you asked for. Neither kind lets an agent do the work and then prove it.

## The thesis

Briefcut splits the job into four parts and gives each part to what does it best.

1. **Meta's models understand the footage.** Muse Spark watches the clips and proposes the story. SAM 3.1 follows the subject. Meta Voice Transcribe hears the speech. Muse Image draws. Muse Wonder watches and listens to the finished cut. ([system context](../ARCHITECTURE.md#1-system-context))
2. **A deterministic compiler executes, on your machine.** Models answer only in bounded JSON. Code checks every answer, puts it on one clock, and compiles it into an FFmpeg program (`render.sh`) that you can read and run again without Briefcut. No model output runs as code. ([the model proposes, the compiler executes](../ARCHITECTURE.md#3-the-model-proposes-the-compiler-executes))
3. **Every claim carries a receipt.** Each check says how it knows: probed from the file, measured by code, or a model's opinion. A version counts only after ffprobe reads the delivered file. In one recorded run, version 1 failed 4 of 16 checks, Briefcut applied 7 patches, and version 2 passed 16 of 16 ([record](evidence/20261031T141719Z-auto-refine.json)).
4. **Any agent can drive it.** Studio, the MCP server, and the CLI reach the same project manager, so a person and an agent can work on the same project. ([agent surface](../ARCHITECTURE.md#9-the-agent-surface))

## Who it is for

- Creators and small teams who cut long footage into shorts and want to see, and fix, every cut.
- Developers and agents that need a video editor they can call, check, and re-run.
- Anyone who must know what left their machine. Every Meta call is listed with its purpose and cost, and a project cannot spend past its cap. ([privacy](PRIVACY.md), [Meta calls and money](../ARCHITECTURE.md#7-meta-calls-and-money))

## What is new

**Models check each other.** A schema makes Spark's answer parse. It does not make it right. In verified find, SAM 3.1 looks for the thing Spark found. When the proof offered Spark's two bowl moments under the search "a dog", SAM found no dog in any of 90 frames and flagged both. When SAM missed a gouge that Spark had found correctly, the default, flag rather than drop, kept the right answer in view ([verified find](evidence/verified-find/README.md)). For the hero word, Spark picked a word and a place for it. SAM's mask showed the bowl would hide 82% of the word there, so code moved it to where 20% is hidden ([hero word](evidence/hero-word/README.md)).

**Honest labels.** "SAM masked 100% of 60 frames" is code measuring SAM's data, so Studio tags it Measured. "The hook works" is a director's judgment, so it is tagged Model opinion, with the model's name. The three words behind the tags, observed, derived, and inferred, are borrowed from the REA project's evidence model ([receipts](../ARCHITECTURE.md#5-receipts-and-evidence-labels)). The receipts are tamper-evident for the same reason: REA's evidence model (MIT) gives each record an id that is the sha256 of its content, and Briefcut does the same, so `vedit verify` names any check, note or byte of video that changed ([receipts you can verify](../ARCHITECTURE.md#receipts-you-can-verify)).

**Wonder hears the cut.** Muse Wonder reviews the finished video with its sound. On the same cut, Wonder failed two checks that the frames confirm, and Spark passed both. In a test with no captions on screen, both models got the words right. Only Wonder described the soundtrack truthfully; Spark invented a lathe sound that is not there. Wonder cost $0.1010 against Spark's $0.0018, so Spark stays the default and Wonder is a choice ([Wonder review](evidence/wonder-review/README.md)).

**Open weights on a laptop.** In private mode, the director's note, caption translation, and the script writer run on Muse Glimmer 30B through llama.cpp on 127.0.0.1. In the recorded run, the Meta API path was replaced by a function that fails the run, and five calls finished at $0 ([private mode](evidence/open-weights/README.md)).

**Meta's agent drives Meta's editor.** Muse Code connects to Briefcut's MCP server and calls its tools ([Muse Code](MUSE_CODE.md)). The recorded run imported a clip and reported its size ([record](evidence/muse-code/README.md)). A full edit driven by Muse Code is not recorded yet.

## What this does not claim

- Every live run used Briefcut's procedural demo clip, so none of them measures quality on real footage.
- The recorded full-loop runs, including the auto-refine run above, predate the Meta-only build. Their planning transcript came from a local Whisper model that has since been removed.
- Meta Voice Transcribe reports turns, not words, so caption word times are estimated.
- The prices are local estimates from the rates in `meta.py`, not Meta's bill.
