"""Live proof of the hero word on the procedural demo clip. Spends real Meta credit (about $0.03).

Run from the repository root, with the key loaded only for this command:

    set -a; . ./.env.local; set +a
    VEDIT_FFMPEG=/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg uv run python docs/evidence/hero-word/proof.py WORK_DIR

Only ``examples/demo/workshop.mp4`` is sent anywhere, and only as a 1 to 3 s
proxy of the window to SAM and as transcript words to Spark. Speech is heard
locally by whisper.cpp, so transcription costs nothing.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from vedit import hero_word, meta
from vedit.edit_spec import default_edit, merge_edit, validate_edit
from vedit.compile import compile_brief
from vedit.ir import Brief, Canvas
from vedit.lint import hero_word_check
from vedit.media import probe, resolve_ffmpeg
from vedit.pipeline import execute, write_codegen, write_report
from vedit.render_plan import brief_sources, build_ops, context

ROOT = Path(__file__).resolve().parents[3]
CLIP = ROOT / "examples" / "demo" / "workshop.mp4"
HERE = Path(__file__).resolve().parent
SUBJECT = "the wooden bowl"
SEGMENT = (0.0, 5.0)
FPS = 30.0
BUDGET = 0.10
SHAPES = {"16:9": ("youtube_1080p", Canvas(width=1920, height=1080, fps=30)), "9:16": ("shorts_9x16", Canvas(width=1080, height=1920, fps=30))}


def whisper_words(work: Path) -> list[dict[str, Any]]:
    """Word times from whisper.cpp, with its sub-word pieces joined back into words."""
    wav, stem = work / "speech.wav", work / "whisper"
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(CLIP), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    model = Path.home() / ".vedit" / "models" / "ggml-base.en.bin"
    subprocess.run(["whisper-cli", "-m", str(model), "-f", str(wav), "-ml", "1", "-oj", "-of", str(stem), "-np"], check=True, capture_output=True)
    pieces = json.loads(stem.with_suffix(".json").read_text(encoding="utf-8"))["transcription"]
    words: list[dict[str, Any]] = []
    for piece in pieces:
        text = piece["text"]
        start, end = piece["offsets"]["from"] / 1000, piece["offsets"]["to"] / 1000
        if not text.strip():
            continue
        if words and not text.startswith(" "):
            words[-1]["text"] += text.strip()
            words[-1]["end"] = max(words[-1]["end"], end)
        else:
            words.append({"start": start, "end": max(end, start + 0.05), "text": text.strip()})
    return words


def sentences(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cues, current = [], []
    for word in words:
        current.append(word)
        if word["text"].endswith((".", "!", "?")):
            cues.append({"start": current[0]["start"], "end": current[-1]["end"], "text": " ".join(w["text"] for w in current), "words": current})
            current = []
    if current:
        cues.append({"start": current[0]["start"], "end": current[-1]["end"], "text": " ".join(w["text"] for w in current), "words": current})
    return cues


def frame_at(video: Path, seconds: float, out: Path) -> Path:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(out)], check=True)
    return out


def sheet(frames: list[Path], out: Path, height: int) -> Path:
    inputs = [arg for frame in frames for arg in ("-i", str(frame))]
    scaled = ";".join(f"[{index}:v]scale=-2:{height}[f{index}]" for index in range(len(frames)))
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *inputs, "-filter_complex",
                    scaled + ";" + "".join(f"[f{index}]" for index in range(len(frames))) + f"hstack=inputs={len(frames)}", "-q:v", "2", str(out)], check=True)
    return out


def luma(video: Path, seconds: float, width: int, height: int) -> bytes:
    run = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-ss", f"{seconds:.3f}", "-i", str(video), "-frames:v", "1",
                          "-vf", f"scale={width}:{height}:flags=neighbor,format=gray", "-f", "rawvideo", "-"], check=True, capture_output=True)
    return run.stdout


def mean_difference(a: bytes, b: bytes) -> float:
    return round(sum(abs(x - y) for x, y in zip(a, b)) / max(1, len(a)), 3)


def per_frame_psnr(layered: Path, tape: Path, work: Path) -> list[float]:
    """PSNR of every frame of the hero_word step against the tape it was given, on raw Y'CbCr with the same tags."""
    stats = work / "psnr.log"
    tags = "setparams=range=limited:color_primaries=bt709:color_trc=bt709:colorspace=bt709"
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(layered), "-i", str(tape), "-lavfi",
                    f"[0:v]{tags}[a];[1:v]{tags}[b];[a][b]psnr=stats_file={stats}", "-f", "null", "-"], check=True)
    values = []
    for line in stats.read_text(encoding="utf-8").splitlines():
        fields = dict(part.split(":", 1) for part in line.split() if ":" in part)
        values.append(float("inf") if fields["psnr_avg"] == "inf" else float(fields["psnr_avg"]))
    return values


def render(plan: dict[str, Any], aspect: str, work: Path, matte: Path | None) -> dict[str, Any]:
    preset, canvas = SHAPES[aspect]
    work.mkdir(parents=True, exist_ok=True)
    ctx = context(plan, request={}, aspect=aspect, canvas=canvas, work=work, has_audio=True, preset=preset, hero_matte=matte)
    ops = build_ops(ctx)
    brief = Brief(name="hero-word", sources=brief_sources(ctx, CLIP), ops=ops, canvas=canvas, hw=False, work_dir=str(work))
    compiler = compile_brief(brief, work_dir=work)
    write_codegen(compiler.pipeline)
    reports = execute(compiler.pipeline, progress=False)
    report = json.loads(write_report(compiler.pipeline, reports, finals=compiler.finals).read_text(encoding="utf-8"))
    video = compiler.finals[0]
    info = probe(video)
    return {"video": video, "ops": [op["op"] for op in ops], "warnings": ctx.warnings + compiler.warnings,
            "hero_layout": compiler.pipeline.meta.get("hero_words"), "steps_ok": all(step["ok"] for step in report["steps"]),
            "proof": {"duration": info.duration, "width": info.width, "height": info.height, "fps": info.fps, "has_audio": info.has_audio,
                      "has_video": info.has_video, "color_space": info.video.color_space if info.video else None,
                      "color_range": info.video.color_range if info.video else None}}


def main(work: Path) -> None:
    started = time.time()
    work.mkdir(parents=True, exist_ok=True)
    words = [word for word in whisper_words(work) if word["start"] < SEGMENT[1]]
    plan: dict[str, Any] = {"segments": [{"start": SEGMENT[0], "end": SEGMENT[1]}], "analysis": {"transcript": sentences(words), "words": words}}
    plan["edit"] = validate_edit(default_edit(), duration=SEGMENT[1] - SEGMENT[0])
    meta_dir = work / "meta"
    suggestion = hero_word.suggest_hero_word(plan, work_dir=meta_dir, budget_usd=BUDGET, aspect="16:9", subject=SUBJECT)
    first = hero_word.build_hero_word_edit(plan, suggestion, subject=SUBJECT, font="heavy", color="#FFFFFF")
    window = hero_word.window_matte(CLIP, plan, first, work_dir=work, budget_usd=BUDGET, fps=FPS, aspect="16:9")

    renders, checks, sheets, edits, sides = {}, {}, {}, {}, {}
    info = probe(CLIP)
    tape_width = info.square_width - info.square_width % 2
    matte_info = probe(window["path"])
    frames = window["stats"]["window_frames"]
    before, inside, after = max(0.1, first["start"] - 0.6), (first["start"] + first["end"]) / 2, min(SEGMENT[1] - 0.1, first["end"] + 0.6)
    for aspect in SHAPES:
        # One SAM matte serves every shape; only the layout, and so the side that reads best, depends on the crop.
        hidden = hero_word.hidden_by_side(window["path"], first["word"], font=first["font"], subject_box=window["subject_box"], frame_width=tape_width,
                                          frame_height=info.display_height, matte_width=matte_info.width, matte_height=matte_info.height,
                                          frames=frames, aspect=aspect)
        side, note = hero_word.choose_side(hidden, suggestion["side"])
        hero = hero_word.build_hero_word_edit(plan, suggestion, subject=SUBJECT, font="heavy", color="#FFFFFF", side=side, subject_box=window["subject_box"])
        shaped = dict(plan, edit=validate_edit(merge_edit(plan["edit"], {"hero_word": hero}), duration=SEGMENT[1] - SEGMENT[0]))
        done = render(shaped, aspect, work / f"render-{aspect.replace(':', 'x')}", window["path"])
        stats = dict(window["stats"], hidden=hidden[side], hidden_by_side=hidden)
        checks[aspect] = hero_word_check(dict(hero, spoken=suggestion["spoken"]), stats, duration=SEGMENT[1] - SEGMENT[0])
        stills = [frame_at(done["video"], moment, work / f"{aspect.replace(':', 'x')}-{label}.jpg")
                  for label, moment in (("before", before), ("inside", inside), ("after", after))]
        sheets[aspect] = sheet(stills, work / f"sheet-{aspect.replace(':', 'x')}.jpg", 360 if aspect == "16:9" else 640)
        renders[aspect], edits[aspect], sides[aspect] = done, hero, {"chosen": side, "note": note, "hidden_by_side": hidden}
    plan["edit"] = validate_edit(merge_edit(plan["edit"], {"hero_word": edits["16:9"]}), duration=SEGMENT[1] - SEGMENT[0])

    # Observed: the same cut without the word, compared frame for frame.
    plain = dict(plan, edit=validate_edit(merge_edit(plan["edit"], {"hero_word": None}), duration=SEGMENT[1] - SEGMENT[0]))
    reference = render(plain, "16:9", work / "render-reference", None)
    size = (192, 108)
    observed = {label: mean_difference(luma(renders["16:9"]["video"], moment, *size), luma(reference["video"], moment, *size))
                for label, moment in (("before", before), ("inside", inside), ("after", after))}

    step = work / "render-16x9"
    psnr = per_frame_psnr(next(step.glob("*_hero_word.mp4")), next(step.glob("*_select_ranges.mp4")), step)
    window_frames = range(renders["16:9"]["hero_layout"][0]["first_frame"], renders["16:9"]["hero_layout"][0]["last_frame"])
    outside = [value for index, value in enumerate(psnr) if index not in window_frames]
    inside_values = [psnr[index] for index in window_frames if index < len(psnr)]
    step_check = {"frames": len(psnr), "window": [window_frames.start, window_frames.stop],
                  "outside_min_psnr_db": round(min(outside), 2), "outside_mean_psnr_db": round(sum(outside) / len(outside), 2),
                  "inside_max_psnr_db": round(max(inside_values), 2), "inside_min_psnr_db": round(min(inside_values), 2)}

    calls = meta._read_ledger(meta_dir)["calls"]
    spend = round(sum(float((call.get("usage") or {}).get("cost_usd", 0)) for call in calls), 6)
    evidence = {
        "schema": "briefcut.hero-word.v1", "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "clip": "examples/demo/workshop.mp4",
        "segment": list(SEGMENT), "subject": SUBJECT, "transcriber": "whisper.cpp ggml-base.en (local, no spend)",
        "spark": {"model": suggestion.get("model"), "proposal": suggestion.get("proposal"), "checked": {key: suggestion[key] for key in ("word", "start", "end", "side", "spoken", "adjusted", "reason")},
                  "usage": suggestion.get("usage")},
        "sam": {"model": "sam-3.1", "usage": window["usage"], "object_id": window["object_id"], "subject_box": window["subject_box"],
                "matte": {key: value for key, value in window["stats"].items() if key not in ("hidden", "hidden_by_side")}},
        "side_by_shape": sides,
        "edit_hero_word": edits,
        "renders": {aspect: {key: (str(value.relative_to(work)) if isinstance(value, Path) else value) for key, value in done.items()} for aspect, done in renders.items()},
        "checks": checks,
        "observed_mean_luma_difference_vs_plain_render": observed,
        "observed_hero_step_vs_its_input_tape": step_check,
        "meta_calls": [{key: call.get(key) for key in ("operation", "model", "status", "request_id", "reserved_usd")} | {"cost_usd": (call.get("usage") or {}).get("cost_usd")} for call in calls],
        "spend_usd": spend, "budget_usd": BUDGET, "seconds": round(time.time() - started, 1),
    }
    HERE.mkdir(parents=True, exist_ok=True)
    (HERE / "evidence.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    for aspect, path in sheets.items():
        shutil.copyfile(path, HERE / f"contact-{aspect.replace(':', 'x')}.jpg")
    print(json.dumps({"word": first["word"], "window": [first["start"], first["end"]], "sides": {aspect: row["chosen"] for aspect, row in sides.items()}, "spend_usd": spend,
                      "checks": {aspect: check["passed"] for aspect, check in checks.items()}, "observed": observed, "step": step_check}, indent=2))


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
