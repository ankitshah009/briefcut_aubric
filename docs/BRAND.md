# Briefcut by Aubric.ai

**Working product name:** Briefcut  
**Attribution:** by Aubric.ai  
**Checkout folder and repository slug:** `briefcut`  
**Promise:** Turn your footage into a story worth watching.

Briefcut is a video editor for shaping real footage: select moments, arrange the story, frame the subject, refine captions, add a deliberate effect, and export the finished cut. The name is the product. A brief is the readable list of decisions. The cut is what those decisions become. Say it as **brief-cut**. Use **Briefcut**, with one capital letter, in prose and app labels. Use **Briefcut by Aubric.ai** on the launch screen, README, product introduction, and demo closing frame.

## Product hierarchy

The story is the primary object. Footage, selected moments, captions, framing, effects, and delivery are parts of that story. Curated 3D-style effects belong inside the video workflow. They do not turn the product into a 3D creation workspace.

Use straightforward feature names such as **Story**, **Captions**, **Framing**, **Effects**, and **Export**. In the effects catalogue, name the visual result: **Floating frame**, **Perspective sway**, **Push-in reveal**. The effects use animated framing and perspective; they do not reconstruct unseen geometry. See [Spatial effects](SPATIAL_EFFECTS.md).

Aubric.ai remains the parent company. Its public developer site describes publishability evaluation for advertising. That creates a plausible future connection between making a creative and evaluating it, but Briefcut's technical export verification is not an Aubric publishability evaluation. Do not claim that integration until it is implemented and verified. [Aubric developer overview](https://aubric.ai/developers)

## Name check

Limited web checks on October 29, 2026. This is an initial collision check, not trademark, domain, app-store, or social-handle clearance. No domain was purchased or account registered.

| Candidate | Assessment |
| --- | --- |
| **Briefcut** | Selected working name. The brief is the source of truth, and the cut is the result. Serious, pronounceable, and specific to this editor. |
| Boopcut | Rejected. It sounds like a sound effect, not an editing tool. |
| Mise | Rejected. Obscure as a product name. |
| Finecut | Rejected. Already a video editor, a Premiere pack, a film sales company, and an NBC app. |
| Stringout | Rejected. stringout.com already assembles local footage into editor timelines. |
| KeepCut | Rejected. An existing private editor already cuts dead air, captions, and reframes. |
| Matchcut, Workprint | Rejected. Both are already product names. |
| Joltcut | No exact-name product found in the limited search, but nearby Jolt video tooling makes its search context less distinct. [JoltCracker](https://splosh.icu/jolt) |
| Jumpli | Rejected because a creator already sells an After Effects tool under this name. [Jumpli motion utilities](https://kuldeepmp4.gumroad.com/l/jumpli) |
| Rifflo | Rejected because it already names an AI creative service and other active products. [Rifflo service description](https://play-rifflo.com/terms/) |
| ZupCut | Rejected because it is already an active consumer brand. [ZupCut](https://zupcut.com/) |
| Fizzcut | Rejected because the name is already used for audio equipment and tools. [ShuttleGear Fizzcut](https://www.shuttlebugrecords.com/Pages/shuttlegear.htm) |

## Local compatibility

The visible product and native bundle are **Briefcut** and **Briefcut.app**. The local checkout folder is `briefcut`. The engine remains `vedit`. Preserve these compatibility surfaces:

- Python imports, package name, and CLI: `vedit`, `python -m vedit`, and `from vedit import Edit`.
- Environment variables beginning with `VEDIT_`.
- Existing briefs, project programs, API routes, and job identifiers.
- The native workspace at `~/Library/Application Support/Vedit`, so existing projects remain discoverable.
- Existing checkout and virtual-environment paths while development and test processes are running.

A repository rename does not require moving the checkout or renaming the Python package. Avoid a broad string replacement of `vedit`: many occurrences are interfaces or storage locations, not visible brand copy.

## GitHub rename plan

Observed repository: `https://github.com/ankitshah009/video_editor.git`, branch `main`. Proposed repository: `ankitshah009/briefcut`. The local folder rename does not rename GitHub. No GitHub change has been executed.

After the specific external rename is approved, use the installed CLI's supported command:

```sh
git branch --show-current
gh repo rename briefcut --repo ankitshah009/video_editor
git remote set-url origin https://github.com/ankitshah009/briefcut.git
git remote get-url origin
```

The `gh repo rename --help` output was checked locally. Keep the owner unchanged; this plan does not transfer the repository to an Aubric organization, change visibility, commit work, or push code. Confirm the renamed repository resolves before updating release, issue, and documentation links. A future organization transfer is a separate action.

Suggested repository description:

> Briefcut by Aubric.ai: shape real footage into finished stories, with local rendering, editable plans, and curated visual effects.

Keep claims tied to current behavior: an exported file is verified technically; a Meta-assisted decision requires a real model run; a spatial effect is a visual treatment; a campaign archive is an export, not a published social post.
