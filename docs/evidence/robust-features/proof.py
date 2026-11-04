"""Live proof that the hero word and verified find hold up off the oak-bowl clip. Spends real Meta credit (well under $0.40).

Run from the repository root, with the key loaded only for this command:

    set -a; . ./.env.local; set +a
    VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg uv run python docs/evidence/robust-features/proof.py WORK_DIR

Only public samples are sent: the two clips in
``~/GitHub/verified-video-search/data/previews/`` and ``examples/demo/workshop.mp4``.
Speech is heard locally by whisper.cpp. A hero word on a clip with no speech is
supplied by this script, and the results say so; the product refuses it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from vedit import hero_word, meta, verify_find
from vedit.compile import compile_brief
from vedit.edit_spec import merge_edit, default_edit, validate_edit, validate_hero_word
from vedit.ir import Brief, Canvas
from vedit.lint import hero_word_check
from vedit.media import probe, resolve_ffmpeg
from vedit.pipeline import execute, write_report
from vedit.render_plan import brief_sources, build_ops, context

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
PREVIEWS = Path.home() / "GitHub" / "verified-video-search" / "data" / "previews"
WHITEHOUSE = PREVIEWS / "whitehouse-walk.mp4"  # 720x720, 4.8 s, no sound: two people walk toward the camera down a white colonnade
WANRONG = PREVIEWS / "wanrong-walk.mp4"  # 1920x1080, 15.6 s, silent track: a small picture of a person walking, inside a black frame
WORKSHOP = ROOT / "examples" / "demo" / "workshop.mp4"  # the procedural demo with speech
FPS = 30.0
FIND_CAP, HERO_CAP = 0.20, 0.08
SHAPES = {"16:9": ("youtube_1080p", Canvas(width=1920, height=1080, fps=30)), "9:16": ("shorts_9x16", Canvas(width=1080, height=1920, fps=30)),
          "1:1": ("square_1x1", Canvas(width=1080, height=1080, fps=30))}
FONT = "/System/Library/Fonts/Supplemental/Arial.ttf"
QUERIES = [
    (WHITEHOUSE, "the woman walking"), (WHITEHOUSE, "someone waving"), (WHITEHOUSE, "the purple tie"), (WHITEHOUSE, "la mujer caminando"),
    (WHITEHOUSE, "走路的男人"), (WHITEHOUSE, "the men in dark suits"), (WHITEHOUSE, "where I mention pricing"), (WHITEHOUSE, "a dog"),
    (WANRONG, "the person walking"),
]


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def whisper_words(clip: Path, work: Path) -> list[dict[str, Any]]:
    """Word times from whisper.cpp, sub-word pieces joined. An empty list when the clip has no sound."""
    if not probe(clip).has_audio:
        return []
    wav, stem = work / f"{clip.stem}.wav", work / f"{clip.stem}-whisper"
    ffmpeg("-i", str(clip), "-ac", "1", "-ar", "16000", str(wav))
    model = Path.home() / ".vedit" / "models" / "ggml-base.en.bin"
    subprocess.run(["whisper-cli", "-m", str(model), "-f", str(wav), "-ml", "1", "-oj", "-of", str(stem), "-np"], check=True, capture_output=True)
    words: list[dict[str, Any]] = []
    for piece in json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))["transcription"]:
        text, start, end = piece["text"], piece["offsets"]["from"] / 1000, piece["offsets"]["to"] / 1000
        if not text.strip():
            continue
        if words and not text.startswith(" "):
            words[-1]["text"] += text.strip()
            words[-1]["end"] = max(words[-1]["end"], end)
        else:
            words.append({"start": start, "end": max(end, start + 0.05), "text": text.strip()})
    return words


def plan_for(segments: list[tuple[float, float]], words: list[dict[str, Any]]) -> dict[str, Any]:
    analysis = {"transcript": [{"start": words[0]["start"], "end": words[-1]["end"], "text": " ".join(w["text"] for w in words), "words": words}],
                "words": words} if words else {}
    plan: dict[str, Any] = {"segments": [{"start": a, "end": b} for a, b in segments], "analysis": analysis}
    plan["edit"] = validate_edit(default_edit(), duration=sum(b - a for a, b in segments))
    return plan


def frame_at(video: Path, seconds: float, out: Path) -> Path:
    ffmpeg("-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out))
    return out


def labelled(image: Path, label: str, out: Path, height: int = 360) -> Path:
    text = label.replace("\\", "").replace(":", "\\:").replace("'", "")
    ffmpeg("-i", str(image), "-vf", f"scale=-2:{height},pad=iw:ih+44:0:0:black,drawtext=fontfile={FONT}:expansion=none:text='{text}':x=8:y=h-34:fontsize=20:fontcolor=white",
           "-q:v", "3", str(out))
    return out


def stack(images: list[Path], out: Path, *, across: bool = True) -> Path:
    if len(images) == 1:
        shutil.copyfile(images[0], out)
        return out
    inputs = [arg for image in images for arg in ("-i", str(image))]
    height = 400
    scaled = ";".join(f"[{i}:v]scale=-2:{height},pad=ceil(iw/2)*2:ih[s{i}]" for i in range(len(images)))
    joined = "".join(f"[s{i}]" for i in range(len(images))) + f"{'hstack' if across else 'vstack'}=inputs={len(images)}"
    ffmpeg(*inputs, "-filter_complex", scaled + ";" + joined, "-q:v", "3", str(out))
    return out


def grid(images: list[Path], out: Path, columns: int = 3) -> Path:
    rows = [images[at:at + columns] for at in range(0, len(images), columns)]
    lines = [stack(row, out.with_name(f"{out.stem}-row{index}.jpg")) for index, row in enumerate(rows)]
    widths = [probe(line).width for line in lines]
    padded = []
    for index, line in enumerate(lines):
        target = out.with_name(f"{out.stem}-pad{index}.jpg")
        ffmpeg("-i", str(line), "-vf", f"pad={max(widths)}:ih:0:0:black", "-q:v", "3", str(target))
        padded.append(target)
    if len(padded) == 1:
        shutil.copyfile(padded[0], out)
    else:
        inputs = [arg for image in padded for arg in ("-i", str(image))]
        ffmpeg(*inputs, "-filter_complex", "".join(f"[{i}:v]" for i in range(len(padded))) + f"vstack=inputs={len(padded)}", "-q:v", "3", str(out))
    return out


def render(plan: dict[str, Any], clip: Path, aspect: str, work: Path, matte: Path | None) -> dict[str, Any]:
    preset, canvas = SHAPES[aspect]
    work.mkdir(parents=True, exist_ok=True)
    ctx = context(plan, request={}, aspect=aspect, canvas=canvas, work=work, has_audio=probe(clip).has_audio, preset=preset, hero_matte=matte)
    ops = build_ops(ctx)
    brief = Brief(name="hero-word", sources=brief_sources(ctx, clip), ops=ops, canvas=canvas, hw=False, work_dir=str(work))
    compiler = compile_brief(brief, work_dir=work)
    reports = execute(compiler.pipeline, progress=False)
    report = json.loads(write_report(compiler.pipeline, reports, finals=compiler.finals).read_text(encoding="utf-8"))
    video = compiler.finals[0]
    info = probe(video)
    return {"video": str(video), "ops": [op["op"] for op in ops], "warnings": ctx.warnings + compiler.warnings,
            "hero_layout": compiler.pipeline.meta.get("hero_words"), "steps_ok": all(step["ok"] for step in report["steps"]),
            "probe": {"duration": info.duration, "width": info.width, "height": info.height, "has_audio": info.has_audio, "has_video": info.has_video,
                      "color_space": info.video.color_space if info.video else None, "color_range": info.video.color_range if info.video else None}}


def attempt(record: dict[str, Any], key: str, action: Any) -> Any:
    try:
        value = action()
        record[key] = {"ok": True}
        return value
    except (hero_word.HeroWordError, meta.MetaError) as exc:
        record[key] = {"ok": False, "refused": str(getattr(exc, "plain", "") or exc)}
        return None


# --- Hero word ---------------------------------------------------------------------------------

def hero_on_footage(work: Path, results: dict[str, Any]) -> list[Path]:
    sheets: list[Path] = []
    scenarios: dict[str, Any] = {}
    results["hero_word"] = scenarios

    # 1. No sound at all: the product refuses, and nothing is sent.
    plan = plan_for([(0.0, probe(WHITEHOUSE).duration)], whisper_words(WHITEHOUSE, work))
    row: dict[str, Any] = {"clip": WHITEHOUSE.name, "heard": []}
    attempt(row, "suggest", lambda: hero_word.suggest_hero_word(plan, work_dir=work / "hero-none" / "meta", budget_usd=HERO_CAP))
    scenarios["no_sound_whitehouse"] = row

    # 2. A silent track that whisper.cpp still "hears": only a filler, so nothing is sent; a classic "Thank you." is refused at the matte.
    heard = whisper_words(WANRONG, work)
    row = {"clip": WANRONG.name, "heard": heard}
    plan = plan_for([(0.0, probe(WANRONG).duration)], heard)
    attempt(row, "suggest", lambda: hero_word.suggest_hero_word(plan, work_dir=work / "hero-silent" / "meta", budget_usd=HERO_CAP))
    invented = [{"start": 0.0, "end": 0.9, "text": "Thank"}, {"start": 0.9, "end": 1.6, "text": "you."}]
    plan = plan_for([(0.0, probe(WANRONG).duration)], invented)
    hero = {"word": "Thank", "start": 0.0, "end": 1.6, "subject": "the person walking", "side": "center", "font": "heavy", "color": "#FFFFFF"}
    row["simulated_transcript"] = invented
    attempt(row, "matte_for_invented_word", lambda: hero_word.window_matte(WANRONG, plan, hero, work_dir=work / "hero-silent", budget_usd=HERO_CAP, fps=FPS))
    scenarios["silent_track_wanrong"] = row

    # 3. Two people on a light background, 1:1 source, three deliveries. The clip has no speech, so the word is supplied here.
    duration = probe(WHITEHOUSE).duration
    plan = plan_for([(0.0, duration)], [])
    for subject in ("the man in the dark suit", "the two people"):
        tag = "man" if "man" in subject else "people"
        row = {"clip": WHITEHOUSE.name, "subject": subject, "word_supplied_by": "this proof script (the clip has no speech)", "shapes": {}}
        for aspect in SHAPES:
            hero = {"word": "WALK", "start": 1.0, "end": 2.8, "subject": subject, "side": "center", "font": "heavy", "color": "#FFFFFF"}
            made = attempt(row["shapes"].setdefault(aspect, {}), "matte",
                           lambda: hero_word.window_matte(WHITEHOUSE, plan, hero, work_dir=work / f"hero-{tag}", budget_usd=HERO_CAP, fps=FPS, aspect=aspect))
            if not made:
                continue
            placed = validate_hero_word(made["edit"], duration=duration)
            shaped = dict(plan, edit=validate_edit(merge_edit(plan["edit"], {"hero_word": placed}), duration=duration))
            done = render(shaped, WHITEHOUSE, aspect, work / f"render-{tag}-{aspect.replace(':', 'x')}", made["path"])
            check = hero_word_check(placed, made["stats"], duration=duration)
            stills = [labelled(frame_at(Path(done["video"]), at, work / f"{tag}-{aspect.replace(':', 'x')}-{label}.jpg"),
                               f"{aspect} {subject} {label} {at:.2f}s", work / f"{tag}-{aspect.replace(':', 'x')}-{label}-l.jpg")
                      for label, at in (("before", 0.5), ("inside", 1.9), ("after", 3.4))]
            sheet = stack(stills, work / f"hero-{tag}-{aspect.replace(':', 'x')}.jpg")
            sheets.append(sheet)
            row["shapes"][aspect].update(edit=placed, side_note=made["side_note"], color_note=made["color_note"], warnings=made["warnings"],
                                         stats={k: v for k, v in made["stats"].items() if k != "sampled_frames"}, usage=made["usage"], render=done,
                                         check=check, sheet=sheet.name)
        scenarios[f"two_people_light_background_{tag}"] = row

    # 4. Speech, then a re-trim after the word was set: refused, then re-anchored with reanchor=True.
    words = [word for word in whisper_words(WORKSHOP, work) if word["start"] < 5.0]
    original = plan_for([(0.0, 5.0)], words)
    row = {"clip": WORKSHOP.name, "heard": " ".join(word["text"] for word in words)}
    suggestion = attempt(row, "suggest", lambda: hero_word.suggest_hero_word(original, work_dir=work / "hero-retrim" / "meta", budget_usd=HERO_CAP,
                                                                             aspect="16:9", subject="the wooden bowl"))
    if suggestion:
        edit = hero_word.build_hero_word_edit(original, suggestion, subject="the wooden bowl")
        row.update(proposal=suggestion.get("proposal"), checked_edit=edit, spark_model=suggestion.get("model"), spark_usage=suggestion.get("usage"))
        trimmed = plan_for([(1.0, 5.0)], words)
        trimmed["edit"] = validate_edit(merge_edit(trimmed["edit"], {"hero_word": edit}), duration=4.0)
        row["status_after_trim"] = hero_word.window_status(trimmed, edit)
        attempt(row, "matte_without_reanchor", lambda: hero_word.window_matte(WORKSHOP, trimmed, edit, work_dir=work / "hero-retrim", budget_usd=HERO_CAP,
                                                                              fps=FPS, aspect="16:9"))
        made = attempt(row, "matte_with_reanchor", lambda: hero_word.window_matte(WORKSHOP, trimmed, edit, work_dir=work / "hero-retrim", budget_usd=HERO_CAP,
                                                                                  fps=FPS, aspect="16:9", reanchor=True))
        if made:
            placed = validate_hero_word(made["edit"], duration=4.0)
            shaped = dict(trimmed, edit=validate_edit(merge_edit(trimmed["edit"], {"hero_word": placed}), duration=4.0))
            done = render(shaped, WORKSHOP, "16:9", work / "render-retrim", made["path"])
            spoken = row["status_after_trim"].get("spoken")
            check = hero_word_check(dict(placed, spoken=spoken) if spoken else placed, made["stats"], duration=4.0)
            middle = (placed["start"] + placed["end"]) / 2
            stills = [labelled(frame_at(Path(done["video"]), at, work / f"retrim-{label}.jpg"), f"re-trimmed cut, {label} {at:.2f}s", work / f"retrim-{label}-l.jpg")
                      for label, at in (("before", max(0.05, placed["start"] - 0.25)), ("inside", middle), ("after", min(3.9, placed["end"] + 0.4)))]
            sheet = stack(stills, work / "hero-retrim-16x9.jpg")
            sheets.append(sheet)
            row.update(placed=placed, warnings=made["warnings"], color_note=made["color_note"], side_note=made["side_note"],
                       stats={k: v for k, v in made["stats"].items() if k != "sampled_frames"}, usage=made["usage"], render=done, check=check, sheet=sheet.name)
    scenarios["retrimmed_after_the_word_was_set_workshop"] = row
    return sheets


# --- Verified find -----------------------------------------------------------------------------

def find_on_footage(work: Path, results: dict[str, Any]) -> Path | None:
    meta_dir = work / "find" / "meta"
    out_dir = work / "find" / "thumbs"
    rows = []
    tiles: list[Path] = []
    for clip, query in QUERIES:
        row: dict[str, Any] = {"clip": clip.name, "query": query, "rule": verify_find.query_subject(query)}
        try:
            answer = meta.find_moments(clip, query=query, duration=probe(clip).duration, work_dir=meta_dir, budget_usd=FIND_CAP)
        except meta.MetaError as exc:
            row["find_error"] = str(getattr(exc, "plain", "") or exc)
            rows.append(row)
            continue
        row.update(spark_moments=answer["moments"], spark_subject=answer.get("subject"), spark_model=answer.get("model"))
        checked = verify_find.verify_moments(answer["moments"], query, clip, meta_dir, FIND_CAP, phrase=answer.get("subject"), phrase_from="muse-spark",
                                             max_moments=3, out_dir=out_dir)
        row["verified"] = {key: checked.get(key) for key in ("kind", "phrase", "phrase_from", "status", "reason", "spent_usd", "receipt", "warnings")}
        row["verified"]["moments"] = [{key: moment.get(key) for key in ("start", "end", "reason", "verified", "coverage", "flagged", "verification", "thumbnail", "score")}
                                      | {"evidence": {k: v for k, v in (moment.get("evidence") or {}).items() if k in ("window", "sampled_frames", "masked_frames", "peak_area", "confidence", "measured")}}
                                      for moment in checked["moments"]]
        for moment in checked["moments"]:
            if moment.get("thumbnail"):
                outcome = "verified" if moment["verified"] else "flagged" if moment.get("flagged") else "weak" if moment["verified"] is False else "unchecked"
                label = f"{clip.stem[:10]} | {query[:24]} | {checked['phrase']} | {moment['coverage']:.0%} {outcome}"
                tiles.append(labelled(Path(moment["thumbnail"]), label, work / "find" / f"tile-{len(tiles):02d}.jpg", height=300))
        rows.append(row)
        print(json.dumps({"query": query, "subject": answer.get("subject"), "moments": len(answer["moments"]), "status": checked["status"],
                          "phrase": checked["phrase"], "phrase_from": checked.get("phrase_from")}, ensure_ascii=False), flush=True)
    results["verified_find"] = rows
    return grid(tiles, work / "find-contact-sheet.jpg") if tiles else None


def main(work: Path) -> None:
    started = time.time()
    work.mkdir(parents=True, exist_ok=True)
    results: dict[str, Any] = {"schema": "briefcut.robust-features.v1", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    sheets = hero_on_footage(work, results)
    find_sheet = find_on_footage(work, results)
    ledgers = {}
    for folder in sorted(set(path.parent for path in work.rglob("meta-usage.json"))):
        calls = meta._read_ledger(folder)["calls"]
        ledgers[str(folder.relative_to(work))] = [{key: call.get(key) for key in ("operation", "model", "status", "request_id", "reserved_usd")}
                                                  | {"cost_usd": (call.get("usage") or {}).get("cost_usd")} for call in calls]
    results["meta_calls"] = ledgers
    results["spend_usd"] = round(sum(float(call["cost_usd"] or 0) for calls in ledgers.values() for call in calls), 6)
    results["seconds"] = round(time.time() - started, 1)
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "results.json").write_text(json.dumps(results, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    for sheet in sheets:
        shutil.copyfile(sheet, HERE / sheet.name)
    if find_sheet:
        shutil.copyfile(find_sheet, HERE / find_sheet.name)
    print(json.dumps({"spend_usd": results["spend_usd"], "seconds": results["seconds"], "sheets": [sheet.name for sheet in sheets]}, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
