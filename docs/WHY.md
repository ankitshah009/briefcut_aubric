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

- **Creators with phone footage and no time**, who want to see, and fix, every cut.
- **Small businesses and teams** making product or process videos, at cents a project, with receipts a second person can check before posting.
- **Educators and podcasters** cutting long recordings into clips and shorts. A project holds up to 2 hours, and long-form planning picks the story across all of it. Live, it cut Tears of Steel, a 12-minute film, into a 44 s trailer and three shorts for $0.34.
- **Developers and agents** that need a video editor they can call, check, and re-run.
- **Anyone with private footage.** Originals stay on the machine, every Meta call is listed with its cost, and private mode keeps three text steps on Muse Glimmer ([privacy](PRIVACY.md), [Meta calls and money](../ARCHITECTURE.md#7-meta-calls-and-money)).

They pick Briefcut when they need to check, script, or audit an edit; the others still lead on libraries, mobile apps, and publishing ([the comparison](../README.md#why-briefcut-instead-of-capcut-descript-opus-clip-or-premiere)).

## Why it grows Meta's model adoption

Every project runs on Meta's models, at $0.0005 to $0.08 per project in the [scenario matrix](evidence/scenario-matrix/README.md), so usage grows with each video. One MCP server makes any agent, Muse Code first, a video editor on Meta's models. Receipts give teams the audit trail they need before a model edits what they publish. The public source is a reference for Meta's models checking each other, and private mode shows Muse Glimmer doing real work on a laptop. Voice Transcribe hears 25 languages. The [README](../README.md#why-this-grows-metas-model-adoption) gives each point with its evidence.

## What is new

**Models check each other.** A schema makes Spark's answer parse. It does not make it right. In verified find, SAM 3.1 looks for the thing Spark found. Offered Spark's two bowl moments under the search "a dog", SAM found no dog in 90 frames and flagged both ([verified find](evidence/verified-find/README.md)). For the hero word, SAM's mask showed the bowl would hide 82% of the word where Spark put it, so code moved it to where 20% is hidden ([hero word](evidence/hero-word/README.md)).

**Honest labels.** "SAM masked 100% of 60 frames" is code measuring SAM's data, so Studio tags it Measured. "The hook works" is a director's judgment, so it is tagged Model opinion. The labels and the content-hashed record ids come from the REA project's evidence model (MIT), so `vedit verify` names any check, note or byte of video that changed ([receipts](../ARCHITECTURE.md#5-receipts-and-evidence-labels), [verify](../ARCHITECTURE.md#receipts-you-can-verify)).

**Wonder hears the cut.** Reviewing one cut with its sound, Muse Wonder failed two checks that the frames confirm, and Spark passed both. Only Wonder described the soundtrack truthfully; Spark invented a lathe sound. Wonder cost $0.1010 against Spark's $0.0018, so Spark stays the default ([Wonder review](evidence/wonder-review/README.md)).

**A long view of long recordings.** By Meta's docs, Muse Spark and Muse Wonder each have a 1,048,576-token context window, and a two-hour talk's transcript is roughly 24,000 tokens (our estimate). Long-form planning watches the video in windows of 10 minutes or less, then one editor call reads the whole transcript and every window's candidates and picks the story across the whole recording. Code still checks every pick ([long videos](../README.md#long-videos-why-a-long-view-matters)).

**Open weights on a laptop.** In private mode, the director's note, caption translation, and the script writer run on Muse Glimmer 30B on this machine. In the recorded run, the Meta API path was replaced by one that fails the run, and five calls finished at $0 ([private mode](evidence/open-weights/README.md)).

**Meta's agent drives Meta's editor.** Muse Code connects to Briefcut's MCP server and calls its tools ([Muse Code](MUSE_CODE.md)). The recorded run imported a clip ([record](evidence/muse-code/README.md)); a full edit by Muse Code is not recorded yet.

## What this does not claim

- Most live runs used Briefcut's procedural demo clip, and the public clips had no real speech, so no run measures quality on a real person's talk.
- The recorded full-loop runs, including the auto-refine run above, predate the Meta-only build, when a local Whisper model, since removed, wrote the planning transcript.
- Meta Voice Transcribe reports turns, not words, so caption word times are estimated.
- The prices are local estimates from the rates in `meta.py`, not Meta's bill.
