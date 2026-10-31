#!/usr/bin/env python3
"""Run the Studio campaign flow end to end over HTTP and record the evidence.

The script starts ``vedit studio`` on a free loopback port, uploads a clip,
creates a campaign, waits for the editable plan, approves that plan unchanged,
waits for the verified render, downloads the video and report, probes the
video, and writes ``docs/evidence/<timestamp>.json`` plus
``docs/evidence/latest.md``. Timings are wall-clock measurements of this run on
this machine. Standard library only.

    uv run python scripts/demo_run.py --clip examples/demo/workshop.mp4
    uv run python scripts/demo_run.py --provider meta --subject "oak bowl" --effect float
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


def _machine(python: str) -> dict:
    ffmpeg = os.environ.get("VEDIT_FFMPEG") or shutil.which("ffmpeg") or "ffmpeg"
    ffmpeg_version = _command([ffmpeg, "-version"]).splitlines()[:1]
    return {
        "hostname_redacted": True,
        "os": f"{platform.system()} {platform.mac_ver()[0] or platform.release()}",
        "machine": platform.machine(),
        "cpu": _command(["sysctl", "-n", "machdep.cpu.brand_string"]) or platform.processor(),
        "memory_bytes": int(_command(["sysctl", "-n", "hw.memsize"]) or 0),
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


def run(args: argparse.Namespace) -> dict:
    python = args.python or str(REPO / ".venv" / "bin" / "python")
    if not Path(python).is_file():
        raise DemoError(f"Python not found at {python}. Create the environment with uv first: uv venv --python 3.13 .venv")
    clip = Path(args.clip).resolve()
    if not clip.is_file():
        raise DemoError(f"Clip not found: {clip}")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    workspace = Path(args.workspace).resolve() if args.workspace else Path(tempfile.mkdtemp(prefix="briefcut-evidence-"))
    workspace.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")
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
            "A local plan is deterministic signal analysis, not model understanding. Meta mode requires live credentials.",
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
            "local_ready": capabilities["local"]["ready"],
            "local_captions": capabilities["local"]["captions"],
            "meta_available": bool(capabilities.get("meta", {}).get("available")),
            "effects_supported": capabilities.get("effects", {}).get("supported"),
        }
        provider = args.provider
        if provider == "meta" and not record["capabilities"]["meta_available"]:
            raise DemoError("Meta mode requested but the server reports Meta unavailable: " + str(capabilities.get("meta", {}).get("reason")))

        started = time.monotonic()
        upload = _json(f"{base}/api/upload?name={urllib.request.quote(clip.name)}", method="POST", body=clip.read_bytes(), content_type="application/octet-stream", timeout=300)
        record["timings_seconds"]["upload"] = round(time.monotonic() - started, 3)

        captions = bool(args.captions) and (capabilities["local"]["captions"] or args.caption_provider == "meta")
        request = {
            "paths": upload["paths"],
            "goal": args.goal,
            "provider": provider,
            "target_duration": args.target,
            "aspect": args.aspect,
            "captions": captions,
            "language": args.language,
            "subject": args.subject if provider == "meta" else "",
            "budget_usd": args.budget,
            "generate_cover": bool(args.generate_cover and provider == "meta"),
            "effect": {"preset": args.effect, "intensity": args.effect_intensity, "start": 0, "duration": args.effect_duration} if args.effect else None,
        }
        if args.caption_provider != "local":
            request["caption_provider"] = args.caption_provider
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
        complete = _wait_for(base, planned["id"], done={"complete"}, timeout=args.timeout, on_update=note)
        record["timings_seconds"]["render"] = round(time.monotonic() - render_started, 3)
        record["timings_seconds"]["total"] = round(time.monotonic() - started, 3)
        result = complete["result"]
        record["result"] = {
            "proof": result.get("proof"),
            "warnings": result.get("warnings"),
            "cover_kind": result.get("cover_kind"),
            "provider": result.get("provider"),
            "caption_provider": result.get("caption_provider", "local" if captions else None),
            "effect": result.get("effect"),
            "tracked_subject": result.get("tracked_subject"),
            "revision": result.get("revision"),
        }
        record["budget"] = complete.get("budget")
        record["timings_server"] = complete.get("timings")

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
    proof = (record.get("result") or {}).get("proof") or {}
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
        + ("A local plan is deterministic signal analysis, not model understanding. " if plan.get("provider") == "local" else "")
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
        "## Verified output",
        "",
        "| Item | Server proof | Local probe of the downloaded file |", "| --- | --- | --- |",
        f"| Duration | {proof.get('duration')} | {probe.get('duration')} |",
        f"| Size | {proof.get('width')}×{proof.get('height')} | {probe.get('width')}×{probe.get('height')} |",
        f"| Audio / video | {proof.get('has_audio')} / {proof.get('has_video')} | {probe.get('has_audio')} / {probe.get('has_video')} |",
        f"| Codec | — | {_probe_video_stream(probe).get('codec')} @ {_probe_video_stream(probe).get('fps')} fps |",
        f"| Color | {proof.get('color_space')} / {proof.get('color_range')} ({proof.get('color_primaries')}/{proof.get('color_transfer')}) | not reported by `vedit probe`; see report.json |",
        f"| Bytes | {proof.get('size_bytes')} | {(record.get('downloaded') or {}).get('video_bytes')} |",
        "",
        f"Meta spend reserved: ${float(budget.get('reserved_usd', 0) or 0):.4f} of a ${float(budget.get('cap_usd', 0) or 0):.2f} cap. "
        "Local mode makes no model calls.",
        "",
        "Warnings from the render: " + ("; ".join((record.get("result") or {}).get("warnings") or []) or "none"),
        "",
        "Full record: `" + str(record.get("json_path", "")) + "`.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clip", default=str(REPO / "examples" / "demo" / "workshop.mp4"))
    parser.add_argument("--goal", default="Make a 20-second vertical story about the oak bowl: open with the finished bowl, then show how it is made.")
    parser.add_argument("--provider", choices=["local", "meta"], default="local")
    parser.add_argument("--target", type=float, default=20)
    parser.add_argument("--aspect", choices=["9:16", "16:9", "1:1"], default="9:16")
    parser.add_argument("--captions", type=lambda value: value.lower() not in {"0", "false", "no"}, default=True, help="request captions when a transcriber is available (default true)")
    parser.add_argument("--caption-provider", choices=["local", "meta"], default="local")
    parser.add_argument("--language", default="en")
    parser.add_argument("--subject", default="", help="Meta mode only: subject to keep in frame")
    parser.add_argument("--budget", type=float, default=0.25)
    parser.add_argument("--generate-cover", action="store_true")
    parser.add_argument("--effect", choices=["float", "tilt", "push"], default=None)
    parser.add_argument("--effect-intensity", type=float, default=0.55)
    parser.add_argument("--effect-duration", type=float, default=3)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("--workspace", default=None, help="server workspace (default: a fresh temp dir)")
    parser.add_argument("--python", default=None, help="interpreter (default: .venv/bin/python)")
    parser.add_argument("--evidence-dir", default=str(EVIDENCE))
    args = parser.parse_args(argv)

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
