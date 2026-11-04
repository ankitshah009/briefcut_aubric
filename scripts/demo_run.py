#!/usr/bin/env python3
"""Run the Studio campaign flow end to end over HTTP and record the evidence.

The script starts ``vedit studio`` on a free loopback port, uploads a clip,
creates a campaign, waits for the editable plan, approves that plan unchanged,
waits for the verified render, downloads the video and report, probes the
video, and writes ``docs/evidence/<timestamp>.json`` plus
``docs/evidence/latest.md``. Timings are wall-clock measurements of this run on
this machine. Standard library only.

Briefcut runs on Meta models only, so this needs a Meta API key
(MODEL_API_KEY with VEDIT_META_ENABLED=1, or a key saved in Settings).

    uv run python scripts/demo_run.py --clip examples/demo/workshop.mp4
    uv run python scripts/demo_run.py --subject "oak bowl" --effect float
    uv run python scripts/demo_run.py --auto-refine 2
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = REPO / "docs" / "evidence"
ACTIVE = {"queued", "analyzing", "rendering"}
# The bundled demo clip has its own brief. Any other clip gets a brief that names no subject,
# so a run on someone else's footage never asks Meta for a bowl that is not there.
WORKSHOP = REPO / "examples" / "demo" / "workshop.mp4"
WORKSHOP_GOAL = "Make a {target:g}-second vertical story about the oak bowl: open with the finished bowl, then show how it is made."
WORKSHOP_NOTE = "Hold the finished bowl a beat longer before the first cut."
GENERIC_GOAL = "Make a {target:g}-second story from this footage: open on the strongest moment, then show what happens, in order."
GENERIC_NOTE = "Hold the opening shot a beat longer before the first cut."


def default_brief(clip: Path, target: float) -> tuple[str, str]:
    """The goal and director's note used when none is given: the demo's own for the demo clip, neutral for any other."""
    try:
        demo = Path(clip).resolve() == WORKSHOP.resolve()
    except OSError:
        demo = False
    goal, note = (WORKSHOP_GOAL, WORKSHOP_NOTE) if demo else (GENERIC_GOAL, GENERIC_NOTE)
    return goal.format(target=target), note


class DemoError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json(url: str, *, method: str = "GET", body: bytes | None = None, content_type: str = "application/json",
          timeout: float = 30) -> dict:
    request = urllib.request.Request(url, data=body, method=method, headers={"Accept": "application/json"})
    if body is not None:
        request.add_header("Content-Type", content_type)
        request.add_header("Content-Length", str(len(body)))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read())
        except ValueError:
            payload = {"ok": False, "error": f"HTTP {exc.code}"}
        raise DemoError(f"{method} {url} -> HTTP {exc.code}: {payload.get('error', payload)}") from None
    if not isinstance(payload, dict) or payload.get("ok") is False:
        raise DemoError(f"{method} {url} refused: {payload}")
    return payload


def _download(url: str, dest: Path, timeout: float = 120) -> Path:
    request = urllib.request.Request(url, headers={"Accept": "*/*"})
    with urllib.request.urlopen(request, timeout=timeout) as response, dest.open("wb") as handle:
        shutil.copyfileobj(response, handle)
    return dest


def _command(argv: list[str]) -> str:
    try:
        return subprocess.run(argv, check=True, capture_output=True, text=True, timeout=30).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _memory_bytes() -> int:
    """Physical memory: sysctl on macOS, sysconf on Linux, 0 when neither answers."""
    value = _command(["sysctl", "-n", "hw.memsize"])
    if value.isdigit():
        return int(value)
    try:
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    except (AttributeError, OSError, ValueError):
        return 0


def _machine(python: str) -> dict:
    ffmpeg = os.environ.get("VEDIT_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
    ffmpeg_version = _command([ffmpeg, "-version"]).splitlines()[:1]
    return {
        "hostname_redacted": True,
        "os": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}",
        "machine": platform.machine(),
        "cpu": _command(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor(),
        "memory_bytes": _memory_bytes(),
        "python": _command([python, "--version"]),
        "ffmpeg": ffmpeg_version[0] if ffmpeg_version else ffmpeg,
        "vedit": _command([python, "-c", "import vedit; print(vedit.__version__)"]),
    }


def _wait_for(base: str, job_id: str, *, done: set[str], timeout: float, on_update=None) -> dict:
    started = time.monotonic()
    last_stage = None
    while True:
        job = _json(f"{base}/api/campaigns/{job_id}")["job"]
        if job.get("stage") != last_stage:
            last_stage = job.get("stage")
            if on_update:
                on_update(job)
        if job["status"] in done:
            return job
        if job["status"] not in ACTIVE:
            raise DemoError(f"Campaign {job_id} ended with status {job['status']}: {job.get('error') or job.get('message')}")
        if time.monotonic() - started > timeout:
            raise DemoError(f"Timed out after {timeout:.0f}s waiting for {sorted(done)}; last stage {last_stage}.")
        time.sleep(0.5)


def _wait_for_loop(base: str, job_id: str, *, timeout: float, on_update=None) -> dict:
    """Follow auto-refine round by round until the server says why it stopped."""
    deadline = time.monotonic() + timeout
    while True:
        job = _wait_for(base, job_id, done={"complete"}, timeout=max(1.0, deadline - time.monotonic()), on_update=on_update)
        if not (job.get("auto_refine") or {}).get("active"):
            return job
        if time.monotonic() > deadline:
            raise DemoError(f"Timed out after {timeout:.0f}s waiting for auto-refine to stop.")
        time.sleep(0.5)


def run(args: argparse.Namespace) -> dict:
    venv_python = REPO / ".venv" / "bin" / "python"
    python = args.python or (str(venv_python) if venv_python.is_file() else sys.executable)
    if not Path(python).is_file():
        raise DemoError(f"Python not found at {python}. Create the environment with uv first: uv sync")
    clip = Path(args.clip).resolve()
    if not clip.is_file():
        raise DemoError(f"Clip not found: {clip}")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    workspace = Path(args.workspace).resolve() if args.workspace else Path(tempfile.mkdtemp(prefix="briefcut-evidence-"))
    workspace.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
    if not env.get("VEDIT_FFMPEG") and Path("/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg").is_file():
        env["VEDIT_FFMPEG"] = "/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg"
    log_path = workspace / "studio.log"
    log = log_path.open("w", encoding="utf-8")
    server = subprocess.Popen([python, "-m", "vedit", "studio", "--port", str(port)], cwd=workspace, env=env, stdout=log, stderr=subprocess.STDOUT)
    record: dict = {
        "schema": "briefcut-evidence/1",
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "machine": _machine(python),
        "workspace": str(workspace),
        "request": {},
        "timings_seconds": {},
        "stages": [],
        "notes": [
            "Timings are wall-clock measurements of this single run, including HTTP polling at 0.5 s intervals.",
            "Every model call is a Meta model: Muse Spark plans, Meta Voice Transcribe captions. Live credentials are required.",
        ],
    }
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                _json(f"{base}/api/health", timeout=2)
                break
            except (DemoError, urllib.error.URLError, OSError):
                if server.poll() is not None or time.monotonic() > deadline:
                    log.flush()
                    raise DemoError("Studio did not start. Log: " + log_path.read_text(encoding="utf-8")[-1200:]) from None
                time.sleep(0.2)
        capabilities = _json(f"{base}/api/capabilities")
        record["capabilities"] = {
            "render_ready": capabilities["render"]["ready"],
            "meta_available": bool(capabilities.get("meta", {}).get("available")),
            "effects_supported": capabilities.get("effects", {}).get("supported"),
        }
        provider = args.provider
        if not record["capabilities"]["meta_available"]:
            raise DemoError("Briefcut needs Meta, and the server reports it unavailable: " + str(capabilities.get("meta", {}).get("reason")))

        started = time.monotonic()
        upload = _json(f"{base}/api/upload?name={urllib.request.quote(clip.name)}", method="POST", body=clip.read_bytes(), content_type="application/octet-stream", timeout=300)
        record["timings_seconds"]["upload"] = round(time.monotonic() - started, 3)

        captions = bool(args.captions)
        request = {
            "paths": upload["paths"],
            "goal": args.goal,
            "provider": provider,
            "target_duration": args.target,
            "aspect": args.aspect,
            "captions": captions,
            "language": args.language,
            "subject": args.subject,
            "budget_usd": args.budget,
            "generate_cover": bool(args.generate_cover),
            "effect": {"preset": args.effect, "intensity": args.effect_intensity, "start": 0, "duration": args.effect_duration} if args.effect else None,
        }
        request["caption_provider"] = args.caption_provider
        if args.review or args.auto_refine:
            request["review"] = True
        if args.auto_refine:
            request["auto_refine"] = args.auto_refine
        record["request"] = request
        stages: list[dict] = record["stages"]

        def note(job: dict) -> None:
            stages.append({"at": round(time.monotonic() - started, 3), "status": job["status"], "stage": job.get("stage"), "message": job.get("message")})
            print(f"  [{stages[-1]['at']:7.2f}s] {job['status']:<10} {job.get('stage')}: {job.get('message')}", flush=True)

        print(f"Planning with provider={provider} on {clip.name} ...", flush=True)
        plan_started = time.monotonic()
        created = _json(f"{base}/api/campaigns", method="POST", body=json.dumps(request).encode())["job"]
        note(created)
        planned = _wait_for(base, created["id"], done={"planned"}, timeout=args.timeout, on_update=note)
        record["timings_seconds"]["plan"] = round(time.monotonic() - plan_started, 3)
        record["job_id"] = planned["id"]
        record["source"] = planned.get("source")
        plan = planned["plan"]
        record["plan"] = {
            "provider": plan.get("provider"),
            "title": plan.get("title"),
            "summary": plan.get("summary"),
            "segments": plan.get("segments"),
            "duration": plan.get("duration"),
            "analysis_method": (plan.get("analysis") or {}).get("method"),
            "analysis_notes": (plan.get("analysis") or {}).get("notes"),
            "warnings": plan.get("warnings"),
            "usage": plan.get("usage"),
        }
        print(f"Plan ready in {record['timings_seconds']['plan']:.2f}s: {len(plan['segments'])} segment(s), {plan.get('duration')}s", flush=True)

        print("Rendering the stored plan unchanged ...", flush=True)
        render_started = time.monotonic()
        _json(f"{base}/api/campaigns/{planned['id']}/render", method="POST", body=b"{}")
        complete = _wait_for_loop(base, planned["id"], timeout=args.timeout, on_update=note)
        record["timings_seconds"]["render"] = round(time.monotonic() - render_started, 3)
        if args.auto_refine:
            record["auto_refine"] = complete.get("auto_refine")
            print(f"Auto-refine: {(complete.get('auto_refine') or {}).get('stopped')}", flush=True)
        record["timings_seconds"]["total"] = round(time.monotonic() - started, 3)
        result = complete["result"]
        record["result"] = {
            "proof": result.get("proof"),
            "warnings": result.get("warnings"),
            "cover_kind": result.get("cover_kind"),
            "provider": result.get("provider"),
            "caption_provider": result.get("caption_provider", "meta" if captions else None),
            "effect": result.get("effect"),
            "tracked_subject": result.get("tracked_subject"),
            "revision": result.get("revision"),
            "review": result.get("review"),
        }
        record["result"]["checks"] = result.get("checks")
        record["result"]["passed"] = result.get("passed")
        record["result"]["total"] = result.get("total")
        record["result"]["patches"] = result.get("patches")
        record["budget"] = complete.get("budget")
        record["timings_server"] = complete.get("timings")

        if args.revise:
            # The self-correcting loop: apply the fixes the receipts or the director proposed and render the next version.
            body = {"mode": args.revise}
            if args.revise == "note":
                body["note"] = args.note
            print(f"Revising with mode={args.revise} ...", flush=True)
            revise_started = time.monotonic()
            try:
                queued = _json(f"{base}/api/campaigns/{planned['id']}/revise", method="POST", body=json.dumps(body).encode(), timeout=120)["job"]
            except DemoError as exc:
                record["revise"] = {"mode": args.revise, "error": str(exc)}
                print(f"  revise refused: {exc}", flush=True)
            else:
                note(queued)
                revised = _wait_for(base, planned["id"], done={"complete"}, timeout=args.timeout, on_update=note)
                record["timings_seconds"]["revise"] = round(time.monotonic() - revise_started, 3)
                record["revise"] = {"mode": args.revise, "note": args.note if args.revise == "note" else None,
                                    "applied_patches": revised.get("applied_patches"), "revision": revised.get("revision"),
                                    "checks": (revised.get("result") or {}).get("checks"), "passed": (revised.get("result") or {}).get("passed"),
                                    "total": (revised.get("result") or {}).get("total"), "proof": (revised.get("result") or {}).get("proof"),
                                    "review": (revised.get("result") or {}).get("review")}
                complete = revised
                result = revised["result"]
                record["budget"] = complete.get("budget")
        record["versions"] = [{key: value for key, value in version.items() if key not in {"video_url", "report_url", "cover_url", "bundle_url", "brief_url"}} for version in complete.get("versions") or []]

        out_dir = workspace / "evidence-downloads"
        out_dir.mkdir(exist_ok=True)
        video = _download(base + result["video_url"], out_dir / "video.mp4")
        report = _download(base + result["report_url"], out_dir / "report.json")
        _download(base + result["bundle_url"], out_dir / "campaign.zip")
        probe_text = subprocess.run([python, "-m", "vedit", "probe", str(video)], check=True, capture_output=True, text=True, timeout=60).stdout
        record["downloaded"] = {
            "video": str(video), "video_bytes": video.stat().st_size,
            "report": str(report), "bundle_bytes": (out_dir / "campaign.zip").stat().st_size,
            "probe": json.loads(probe_text),
        }
        try:
            report_json = json.loads(report.read_text(encoding="utf-8"))
            record["report_proof"] = report_json.get("proof")
            record["report_steps"] = len(report_json.get("steps", [])) if isinstance(report_json.get("steps"), list) else None
        except (ValueError, OSError):
            record["report_proof"] = None
        return record
    finally:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
        log.close()


def _fmt_seconds(value) -> str:
    return "n/a" if value is None else f"{float(value):.3f} s"


def _probe_video_stream(probe: dict) -> dict:
    for stream in probe.get("streams") or []:
        if isinstance(stream, dict) and stream.get("type") == "video":
            return stream
    return {}


def write_markdown(record: dict, path: Path) -> None:
    source = record.get("source") or {}
    revise = record.get("revise") or {}
    # The downloaded file is the newest verified version; compare it with that version's proof.
    proof = (revise.get("proof") if not revise.get("error") else None) or (record.get("result") or {}).get("proof") or {}
    final_version = revise.get("revision") if proof is revise.get("proof") else (record.get("result") or {}).get("revision")
    probe = (record.get("downloaded") or {}).get("probe") or {}
    timings = record.get("timings_seconds", {})
    plan = record.get("plan") or {}
    budget = record.get("budget") or {}
    lines = [
        "# Latest measured run",
        "",
        f"Recorded {record['recorded_at']} by `scripts/demo_run.py`. One run, one machine. "
        "Timings are wall-clock and include HTTP polling. Re-run with `uv run python scripts/demo_run.py` to refresh.",
        "",
        "## Machine",
        "",
        "| Item | Value |", "| --- | --- |",
        f"| OS | {record['machine'].get('os')} ({record['machine'].get('machine')}) |",
        f"| CPU | {record['machine'].get('cpu')} |",
        f"| Memory | {record['machine'].get('memory_bytes', 0) / 1024**3:.0f} GiB |",
        f"| Python | {record['machine'].get('python')} |",
        f"| FFmpeg | {record['machine'].get('ffmpeg')} |",
        f"| vedit | {record['machine'].get('vedit')} |",
        "",
        "## Request",
        "",
        "| Item | Value |", "| --- | --- |",
        f"| Clip | `{Path(str(record.get('request', {}).get('paths', ['?'])[0])).name}` |",
        f"| Source | {source.get('duration', 0):.2f} s, {source.get('width')}×{source.get('height')}, audio={source.get('has_audio')} |",
        f"| Provider | {record['request'].get('provider')} |",
        f"| Target | {record['request'].get('target_duration')} s, {record['request'].get('aspect')} |",
        f"| Captions | {record['request'].get('captions')} ({(record.get('result') or {}).get('caption_provider') or 'none'}) |",
        f"| Effect | {record['request'].get('effect')} |",
        f"| Subject tracking | {record['request'].get('subject') or 'off'} |",
        "",
        "## Timings",
        "",
        "| Stage | Seconds |", "| --- | --- |",
        f"| Upload | {_fmt_seconds(timings.get('upload'))} |",
        f"| Time to editable plan | {_fmt_seconds(timings.get('plan'))} |",
        f"| Time to verified render | {_fmt_seconds(timings.get('render'))} |",
        f"| Total | {_fmt_seconds(timings.get('total'))} |",
        "",
        "## Plan",
        "",
        f"Provider `{plan.get('provider')}`, analysis `{plan.get('analysis_method') or 'none recorded'}`. "
        + f"{len(plan.get('segments') or [])} segment(s), {plan.get('duration')} s.",
        "",
        "| # | Source range | Label | Reason |", "| --- | --- | --- | --- |",
    ]
    for index, segment in enumerate(plan.get("segments") or [], 1):
        lines.append(f"| {index} | {segment['start']:.2f}–{segment['end']:.2f} s | {segment.get('label', '')} | {segment.get('reason', '')} |")
    for warning in plan.get("warnings") or []:
        lines.append(f"\n> {warning}")
    lines += [
        "",
        f"## Verified output (version {final_version})",
        "",
        "| Item | Server proof | Local probe of the downloaded file |", "| --- | --- | --- |",
        f"| Duration | {proof.get('duration')} | {probe.get('duration')} |",
        f"| Size | {proof.get('width')}×{proof.get('height')} | {probe.get('width')}×{probe.get('height')} |",
        f"| Audio / video | {proof.get('has_audio')} / {proof.get('has_video')} | {probe.get('has_audio')} / {probe.get('has_video')} |",
        f"| Codec | — | {_probe_video_stream(probe).get('codec')} @ {_probe_video_stream(probe).get('fps')} fps |",
        f"| Color | {proof.get('color_space')} / {proof.get('color_range')} ({proof.get('color_primaries')}/{proof.get('color_transfer')}) | not reported by `vedit probe`; see report.json |",
        f"| Bytes | {proof.get('size_bytes')} | {(record.get('downloaded') or {}).get('video_bytes')} |",
        "",
        f"Meta spend reserved: ${float(budget.get('reserved_usd', 0) or 0):.4f} of a ${float(budget.get('cap_usd', 0) or 0):.2f} cap.",
        "",
        "Warnings from the render: " + ("; ".join((record.get("result") or {}).get("warnings") or []) or "none"),
        "",
    ]
    checks = (record.get("result") or {}).get("checks") or []
    if checks:
        lines += ["## Receipts (checks on the first verified render)", "",
                  f"{(record.get('result') or {}).get('passed')} of {(record.get('result') or {}).get('total')} checks passed.", "",
                  "| Check | Result | At | Note |", "| --- | --- | --- | --- |"]
        for check in checks:
            where = "—" if check.get("at") is None else f"{float(check['at']):.1f} s"
            lines.append(f"| {check.get('name')} | {'pass' if check.get('passed') else 'FAIL'} | {where} | {check.get('note', '')} |")
        lines.append("")
    if revise:
        lines += ["## Self-correcting loop", ""]
        if revise.get("error"):
            lines.append(f"Revise (`{revise.get('mode')}`) was refused: {revise['error']}")
        else:
            lines.append(f"Mode `{revise.get('mode')}`" + (f" with note: \"{revise.get('note')}\"" if revise.get("note") else "") + f". Version {revise.get('revision')} rendered in {_fmt_seconds(timings.get('revise'))} and passed {revise.get('passed')} of {revise.get('total')} checks.")
            lines += [""] + [f"- {patch.get('op')} moment {patch.get('segment')}: {patch.get('before')} → {patch.get('after')}" + (f" (skipped: {patch['skipped']})" if patch.get("skipped") else "") + (f" — {patch.get('reason')}" if patch.get("reason") else "") for patch in revise.get("applied_patches") or []]
        lines.append("")
    loop = record.get("auto_refine") or {}
    if loop:
        lines += ["## Auto-refine", "", f"Rounds run: {loop.get('rounds')} of {loop.get('limit')}. {loop.get('stopped')}", ""]
    versions = record.get("versions") or []
    if len(versions) > 1:
        lines += ["## Versions", "", "| v | Source | Status | Checks | Duration |", "| --- | --- | --- | --- | --- |"]
        for version in versions:
            proof_v = version.get("proof") or {}
            lines.append(f"| {version.get('revision')} | {version.get('source')} | {version.get('status')} | {version.get('passed')}/{version.get('total')} | {proof_v.get('duration', '—')} |")
        lines.append("")
    review = (record.get("result") or {}).get("review")
    if review:
        lines += ["## Director's notes (Muse Spark)", ""]
        if review.get("error"):
            lines.append(f"Review unavailable: {review['error']}")
        else:
            lines.append(f"Hook score {review.get('hook_score')}/5. {review.get('verdict', '')}")
            lines += [""] + [f"- Working: {item}" for item in review.get("strengths") or []]
            lines += [f"- Fix: {fix.get('what')}" + (f" (at {fix['where_seconds']:.1f}s)" if fix.get("where_seconds") is not None else "") for fix in review.get("fixes") or []]
            if review.get("caption_suggestion"):
                lines += ["", f"Suggested caption: {review['caption_suggestion']}"]
            usage = review.get("usage") or {}
            if usage.get("cost_usd") is not None:
                lines += ["", f"Review cost: ${float(usage['cost_usd']):.4f} ({review.get('model')})."]
        lines.append("")
    lines += [
        "Full record: `" + str(record.get("json_path", "")) + "`.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clip", default=str(WORKSHOP))
    parser.add_argument("--goal", default=None, help="editorial brief (default: the demo's brief for the demo clip, a neutral one for any other clip)")
    parser.add_argument("--provider", choices=["meta"], default="meta", help="always meta: Briefcut runs on Meta models only")
    parser.add_argument("--target", type=float, default=25)
    parser.add_argument("--aspect", choices=["9:16", "16:9", "1:1", "4:5"], default="9:16")
    parser.add_argument("--captions", type=lambda value: value.lower() not in {"0", "false", "no"}, default=True, help="captions by Meta Voice Transcribe (default true)")
    parser.add_argument("--caption-provider", choices=["meta"], default="meta", help="always meta")
    parser.add_argument("--language", default="en")
    parser.add_argument("--subject", default="", help="subject for SAM 3.1 to keep in frame")
    parser.add_argument("--budget", type=float, default=0.25)
    parser.add_argument("--generate-cover", action="store_true")
    parser.add_argument("--review", action="store_true", help="ask Muse Spark for director's notes on the finished cut (Meta credentials required)")
    parser.add_argument("--effect", choices=["float", "tilt", "push"], default=None)
    parser.add_argument("--effect-intensity", type=float, default=0.55)
    parser.add_argument("--effect-duration", type=float, default=3)
    parser.add_argument("--revise", choices=["auto", "director", "note"], default=None, help="after the first verified render, apply fixes and render the next version")
    parser.add_argument("--note", default=None, help="director's note for --revise note (default follows --goal's rule)")
    parser.add_argument("--auto-refine", type=int, choices=[0, 1, 2, 3], default=0, help="after a verified render, let the worker apply the fixes and render up to this many more versions (turns --review on)")
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--workspace", default=None, help="server workspace (default: a fresh temp dir)")
    parser.add_argument("--python", default=None, help="interpreter (default: .venv/bin/python)")
    parser.add_argument("--evidence-dir", default=str(EVIDENCE))
    args = parser.parse_args(argv)
    goal, note = default_brief(Path(args.clip), args.target)
    args.goal = args.goal or goal
    args.note = args.note or note

    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = evidence / f"{stamp}.json"
    try:
        record = run(args)
    except DemoError as exc:
        print(f"demo_run failed: {exc}", file=sys.stderr)
        return 2
    record["json_path"] = str(json_path.relative_to(REPO)) if json_path.is_relative_to(REPO) else str(json_path)
    json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_markdown(record, evidence / "latest.md")
    timings = record["timings_seconds"]
    print(f"\nPlan {timings['plan']:.2f}s · Render {timings['render']:.2f}s · Total {timings['total']:.2f}s")
    print(f"Evidence: {json_path} and {evidence / 'latest.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
