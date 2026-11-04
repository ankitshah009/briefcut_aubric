#!/usr/bin/env python3
"""Exercise Studio through the installed gstack browser and verify real artifacts.

Run against a fresh Studio process, while no other agent is using gstack browse:
    uv run python scripts/qa_studio.py --clip examples/demo/talk.mp4

No browser installation, paid API calls, or mocked render results. The browser is
shared by gstack, so this script creates a new tab and leaves its evidence there.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from vedit.media import probe  # noqa: E402


class CheckFailure(RuntimeError):
    pass


def browser_binary(explicit: str | None) -> Path:
    candidates = [Path(explicit)] if explicit else []
    if os.environ.get("GSTACK_BROWSE"):
        candidates.append(Path(os.environ["GSTACK_BROWSE"]))
    candidates += [
        ROOT / ".claude/skills/gstack/browse/dist/browse",
        Path.home() / ".claude/skills/gstack/browse/dist/browse",
        Path.home() / ".agents/skills/gstack/browse/dist/browse",
    ]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    raise CheckFailure("gstack browse is not installed. Set GSTACK_BROWSE to its existing executable.")


def unwrap(output: str) -> str:
    """Remove only gstack's known envelopes; returned page content is data."""
    lines = output.strip().splitlines()
    begin = next((i for i, line in enumerate(lines) if "BEGIN UNTRUSTED" in line), None)
    end = next((i for i, line in enumerate(lines) if "END UNTRUSTED" in line), None)
    if begin is not None and end is not None and end > begin:
        return "\n".join(lines[begin + 1:end]).strip()
    return output.strip()


class Browser:
    def __init__(self, binary: Path, out: Path):
        self.binary, self.out = binary, out
        self.transcript = out / "browser-commands.jsonl"

    def run(self, command: str, *args: str, timeout: float = 40) -> str:
        started = time.monotonic()
        result = subprocess.run(
            [str(self.binary), command, *map(str, args)], cwd=ROOT,
            text=True, capture_output=True, timeout=timeout, check=False,
        )
        # Commands contain only fixture paths and test text, never credentials.
        with self.transcript.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "command": command, "args": list(args), "exit_code": result.returncode,
                "duration_seconds": round(time.monotonic() - started, 3),
                "stdout": result.stdout, "stderr": result.stderr,
            }) + "\n")
        if result.returncode:
            raise CheckFailure(f"browse {command} failed: {(result.stderr or result.stdout).strip()[:1200]}")
        return unwrap(result.stdout)

    def value(self, expression: str) -> Any:
        raw = self.run("js", "JSON.stringify(" + expression + ")")
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CheckFailure(f"Browser returned unreadable test data: {raw[:500]}") from exc

    def check(self, expression: str, message: str) -> None:
        if self.value(expression) is not True:
            raise CheckFailure(message)

    def until(self, expression: str, message: str, timeout: float = 25) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.value(expression) is True:
                return
            time.sleep(0.4)
        diagnostic = self.value("({status:document.querySelector('#project-status')?.textContent,error:document.querySelector('#error-text')?.textContent,recovery:document.querySelector('#recovery-message')?.textContent})")
        raise CheckFailure(f"{message}: {diagnostic}")

    def job(self) -> dict[str, Any]:
        raw = self.run("js", "(async()=>{const id=localStorage.getItem('vedit.studio.current');if(!id)throw new Error('No current project');const response=await fetch('/api/campaigns/'+encodeURIComponent(id));if(!response.ok)throw new Error('Cannot load current project');return JSON.stringify((await response.json()).job)})()")
        return json.loads(raw)

    def screenshot(self, name: str) -> None:
        self.run("screenshot", str(self.out / name))

    def playback(self, name: str, *, trigger: str | None = None, start: float = 0) -> dict[str, Any]:
        """Prove decoded frames and advancing playback, then leave a still frame."""
        if trigger:
            self.run("click", trigger)
        else:
            self.run("js", "(async()=>{const player=document.querySelector('#player');player.currentTime=" + json.dumps(start) + ";await player.play();return 'Playback started'})()")
        self.until(
            "document.querySelector('#player').readyState>=2&&document.querySelector('#player').currentTime>" + str(start + .1),
            name + " did not decode and advance during playback",
        )
        initial_time = self.value("document.querySelector('#player').currentTime")
        self.until(
            "!document.querySelector('#player').paused&&document.querySelector('#player').readyState>=2&&document.querySelector('#player').currentTime>" + str(initial_time + .1),
            name + " did not continue playing between observations",
        )
        state = self.value("(()=>{const player=document.querySelector('#player');player.pause();return {ready_state:player.readyState,current_time:player.currentTime,width:player.videoWidth,height:player.videoHeight,error:player.error?.message||null}})()")
        state["initial_time"] = initial_time
        if state.get("error") or not state.get("width") or not state.get("height"):
            raise CheckFailure(f"{name} has no playable picture: {state}")
        return state


def assert_video(path: Path, expected_duration: float, expected_audio: bool) -> dict[str, Any]:
    info = probe(path)
    if not info.has_video or info.width != 1080 or info.height != 1080:
        raise CheckFailure("Downloaded video did not have the requested 1080×1080 picture.")
    if info.has_audio != expected_audio:
        raise CheckFailure("Downloaded video did not preserve the source's audio presence.")
    if abs(info.duration - expected_duration) > 0.3:
        raise CheckFailure(f"Downloaded duration {info.duration} does not match the edited range {expected_duration}.")
    if not info.video or info.video.color_space != "bt709" or info.video.color_range != "tv":
        raise CheckFailure("Downloaded video lacks the expected BT.709 limited-range color tags.")
    return {"duration": info.duration, "width": info.width, "height": info.height,
            "has_audio": info.has_audio, "has_video": info.has_video,
            "color_space": info.video.color_space, "color_range": info.video.color_range}


def run(args: argparse.Namespace) -> dict[str, Any]:
    parsed = urlparse(args.url)
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or parsed.username or parsed.password:
        raise CheckFailure("Use a local Studio URL, such as http://127.0.0.1:8765. Remote environments are not supported.")
    source = args.clip.resolve()
    if not source.is_file():
        raise CheckFailure(f"The local video fixture does not exist: {source}")
    source_info = probe(source)
    if not source_info.has_video or source_info.duration < 3:
        raise CheckFailure("Choose a playable video fixture at least three seconds long.")
    if source.stat().st_size > 200_000_000:
        raise CheckFailure("The video fixture must be smaller than 200 MB.")
    args.out.mkdir(parents=True, exist_ok=True)
    out = args.out.resolve()
    browser = Browser(browser_binary(args.browser), out)
    evidence: dict[str, Any] = {"started_at": datetime.now(timezone.utc).isoformat(), "url": args.url, "fixture": source.name, "checks": []}

    def passed(name: str) -> None:
        evidence["checks"].append(name)
        print("PASS " + name, flush=True)

    try:
        # Probe installed flags before using them. This does not install a browser.
        help_text = browser.run("--help")
        for command in ("upload", "select", "console", "newtab"):
            if command not in help_text:
                raise CheckFailure(f"Installed gstack browse does not advertise {command}.")
        # Some installed versions omit extraction commands from their short help.
        # Missing-argument validation proves this command exists without fetching.
        download_probe = subprocess.run([str(browser.binary), "download"], cwd=ROOT, text=True, capture_output=True, timeout=15, check=False)
        if "Usage: download <url|@ref>" not in download_probe.stdout + download_probe.stderr:
            raise CheckFailure("Installed gstack browse did not recognize the download command.")
        browser.run("newtab", args.url)
        browser.run("viewport", "1280x900")
        browser.run("console", "--clear")
        browser.until("document.querySelector('#connection-status')?.textContent==='Editor ready'", "Studio did not become ready")
        browser.run("click", "#new-project")
        browser.run("upload", "#clip", str(source))
        browser.until("document.querySelector('#player').readyState>=1", "Local source preview did not load")
        evidence["source_playback"] = browser.playback("Local source")
        passed("local upload immediately previews the fixture")

        browser.run("fill", "#goal", "Keep a short, clear source moment for the browser regression check.")
        browser.run("select", "#aspect", "1:1")
        browser.run("click", "#provider-local")
        if browser.value("document.querySelector('#captions').checked"):
            browser.run("click", "#captions")
        browser.run("click", "#plan")
        browser.until("!document.querySelector('#story-content').hidden&&!document.querySelector('#make').disabled", "Story was not created")
        browser.check("document.querySelector('#provider-local').checked", "The QA flow must never select a cloud provider.")
        passed("local story plan is editable without a model API")

        browser.run("fill", "#segment-0-start", "0.5")
        browser.run("fill", "#segment-0-end", "2.5")
        browser.run("fill", "#story-title", "Browser-verified campaign")
        browser.run("fill", "#post-caption", "A real edit verified through the Studio browser flow.")
        browser.run("fill", "#hashtags", "#studio #verified")
        effects_available = browser.value("!document.querySelector('#effects-section').hidden")
        if effects_available:
            browser.run("select", "#effect-preset", "push")
            browser.run("fill", "#effect-start", "0.2")
            browser.run("fill", "#effect-duration", "1.2")
            browser.check("!document.querySelector('#make').disabled", "Changing only an effect must not require a new model-generated story.")
        browser.run("select", "#aspect", "16:9")
        browser.check("document.querySelector('#make').disabled", "Changing source settings must block a stale render.")
        browser.run("select", "#aspect", "1:1")
        browser.check("!document.querySelector('#make').disabled", "Restoring planned settings should make the approved edit renderable.")
        passed("changed source settings cannot silently render an old plan")
        browser.run("reload")
        browser.until("document.querySelector('#segment-0-end')?.value==='2.5'", "Edited range was not recovered after reload")
        browser.check("document.querySelector('#segment-0-start').value==='0.5'&&document.querySelector('#story-title').value==='Browser-verified campaign'", "Draft title or in point did not survive reload.")
        if effects_available:
            browser.check("document.querySelector('#effect-preset').value==='push'&&document.querySelector('#effect-start').value==='0.2'&&document.querySelector('#effect-duration').value==='1.2'", "Draft effect settings did not survive reload.")
        browser.until("document.querySelector('#player').readyState>=1", "Saved source preview did not reload")
        evidence["saved_source_playback"] = browser.playback("Saved source moment", trigger=".segment-preview", start=.5)
        browser.screenshot("story.png")
        passed("edited timings and copy survive a page reload")

        browser.run("click", "#make")
        browser.until("document.querySelector('#project-status').textContent==='Verified'", "Render did not complete", args.timeout)
        browser.until("!document.querySelector('#delivery-panel').hidden", "Verified delivery did not appear")
        job = browser.job()
        if job.get("status") != "complete" or job.get("request", {}).get("provider") != "local":
            raise CheckFailure("Render did not complete through the local provider.")
        if not job.get("result") or job["result"].get("stale"):
            raise CheckFailure("First completed render is unexpectedly stale.")
        segments = job.get("plan", {}).get("segments", [])
        if len(segments) != 1 or segments[0]["start"] != .5 or segments[0]["end"] != 2.5:
            raise CheckFailure("First render did not use the exact edited source range, 0.5–2.5 seconds.")
        if effects_available and (job["result"].get("effect") or {}).get("preset") != "push":
            raise CheckFailure("Verified delivery does not record the approved push-in effect.")
        first_revision = job["result"].get("revision", job.get("verified_revision", job.get("revision")))
        evidence["job_id"] = job["id"]
        evidence["verified_revision"] = first_revision
        (out / "completed-job.json").write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
        video_url = browser.value("document.querySelector('#download-video').href")
        bundle_url = browser.value("document.querySelector('#download-bundle').href")
        browser.run("download", video_url, str(out / "video.mp4"))
        browser.run("download", bundle_url, str(out / "campaign.zip"))
        evidence["proof"] = assert_video(out / "video.mp4", 2, source_info.has_audio)
        digest = hashlib.sha256((out / "video.mp4").read_bytes()).hexdigest()
        with zipfile.ZipFile(out / "campaign.zip") as bundle:
            names = set(bundle.namelist())
            if not {"video.mp4", "brief.yaml", "report.json", "story.json", "post.txt"}.issubset(names) or not any(name.startswith("cover.") for name in names):
                raise CheckFailure("Campaign ZIP is missing a deliverable.")
            report = json.loads(bundle.read("report.json"))
            if abs(report.get("proof", {}).get("duration", 0) - 2) > .3:
                raise CheckFailure("Bundled verification report does not describe the edited duration.")
            if effects_available and (report.get("effect") or {}).get("preset") != "push":
                raise CheckFailure("Bundled report does not identify the rendered effect.")
            if hashlib.sha256(bundle.read("video.mp4")).hexdigest() != digest:
                raise CheckFailure("Bundle and video download do not contain the same verified render.")
        browser.run("click", "#preview-final")
        evidence["finished_playback"] = browser.playback("Finished video")
        browser.screenshot("delivery.png")
        passed("actual downloaded video and campaign bundle match the approved edit")
        passed("source, saved source range, and finished video decode and advance in the browser")
        if effects_available:
            passed("effect edits persist and render without a second story-planning request")

        if not args.skip_cancellation:
            # A long second edit gives the UI time to cancel a real queued/active
            # render. The first, short deliverable must remain available.
            render_range = min(source_info.duration, 20)
            browser.run("fill", "#segment-0-start", "0")
            browser.run("fill", "#segment-0-end", str(render_range))
            for index in range(1, 8):
                browser.run("click", "#add-segment")
                browser.run("fill", f"#segment-{index}-start", "0")
                browser.run("fill", f"#segment-{index}-end", str(render_range))
            browser.check("Number(document.querySelector('#segment-0-start').value)===0&&Number(document.querySelector('#segment-0-end').value)===" + str(render_range), "Editing a rendered moment was lost when another moment was added.")
            browser.run("fill", "#story-title", "Cancelled replacement attempt")
            if effects_available:
                browser.run("select", "#effect-preset", "tilt")
            browser.run("click", "#make")
            browser.until("!document.querySelector('#cancel-job').hidden&&!document.querySelector('#cancel-job').disabled", "Replacement render did not enter a cancellable state")
            browser.check("!document.querySelector('#delivery-panel').hidden&&document.querySelector('#delivery-panel').classList.contains('is-previous')", "Previous verified output disappeared during rerender.")
            browser.run("click", "#cancel-job")
            browser.until("document.querySelector('#project-status').textContent==='Cancelled'", "Cancellation did not stop the render", 45)
            browser.check("!document.querySelector('#delivery-panel').hidden&&!document.querySelector('#preview-final').disabled&&!document.querySelector('#make').disabled", "Cancelled replacement must retain previous delivery and allow an edited retry.")
            browser.check("document.querySelector('#delivery-description').textContent.includes('cancelled')&&document.querySelector('#preview-final').textContent==='Previous verified video'", "UI did not distinguish the cancelled attempt from the previous verified video.")
            if effects_available:
                browser.check("document.querySelector('#delivery-summary').textContent.includes('Push-in reveal')", "Previous output was mislabeled with the new attempt's effect.")
            cancelled = browser.job()
            if cancelled.get("status") != "cancelled" or not cancelled.get("result", {}).get("stale") or cancelled["result"].get("revision") != first_revision:
                raise CheckFailure("Backend did not preserve the verified revision after cancellation.")
            segments = cancelled.get("plan", {}).get("segments", [])
            if len(segments) != 8 or any(segment["start"] != 0 or abs(segment["end"] - render_range) > .001 for segment in segments):
                raise CheckFailure("The replacement render did not receive all edited moment timings.")
            passed("moments remain editable after a render and retain changes when adding more moments")
            browser.run("download", browser.value("document.querySelector('#download-video').href"), str(out / "preserved-video.mp4"))
            if hashlib.sha256((out / "preserved-video.mp4").read_bytes()).hexdigest() != digest:
                raise CheckFailure("The previous verified download changed after a cancelled replacement.")
            browser.run("reload")
            browser.until("document.querySelector('#project-status').textContent==='Cancelled'&&!document.querySelector('#delivery-panel').hidden", "Cancelled attempt lost its verified result after reload")
            browser.run("click", "#preview-final")
            evidence["preserved_playback"] = browser.playback("Previous verified video")
            browser.screenshot("cancelled-with-preserved-video.png")
            (out / "cancelled-job.json").write_text(json.dumps(cancelled, indent=2) + "\n", encoding="utf-8")
            passed("cancelled rerender preserves the exact previous video and truthful UI across reload")

        browser.run("viewport", "390x844")
        browser.check("document.documentElement.scrollWidth<=window.innerWidth", "Mobile page has horizontal overflow.")
        browser.screenshot("mobile.png")
        passed("mobile layout has no horizontal overflow")
        errors = browser.run("console", "--errors")
        (out / "console.txt").write_text(errors + "\n", encoding="utf-8")
        if errors != "(no console errors)":
            raise CheckFailure("Browser console contains errors or warnings; inspect console.txt.")
        passed("browser console has no errors or warnings")
        evidence["status"] = "passed"
        return evidence
    except BaseException as exc:
        evidence["status"] = "failed"
        evidence["error"] = str(exc)
        try:
            browser.screenshot("failure.png")
            (out / "failure-console.txt").write_text(browser.run("console", "--errors") + "\n", encoding="utf-8")
        except Exception:
            pass
        raise
    finally:
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat()
        (out / "report.json").write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--clip", type=Path, required=True, help="Local video fixture, at least three seconds; no paid API is used")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Running local Studio URL")
    parser.add_argument("--out", type=Path, default=ROOT / "out" / ("qa-studio-" + datetime.now().strftime("%Y%m%d-%H%M%S")))
    parser.add_argument("--browser", help="Existing gstack browse executable (defaults to GSTACK_BROWSE or installed paths)")
    parser.add_argument("--timeout", type=float, default=180, help="Maximum seconds to wait for a render")
    parser.add_argument("--skip-cancellation", action="store_true", help="Skip the second-render cancellation regression")
    args = parser.parse_args()
    if args.timeout <= 0 or args.timeout > 1800:
        parser.error("--timeout must be between 0 and 1800 seconds")
    # Prevent two copies of this regression runner from driving the shared tab.
    with (Path(tempfile.gettempdir()) / "vedit-browser-qa.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("Another Studio browser regression is running.")
        try:
            result = run(args)
        except (CheckFailure, subprocess.TimeoutExpired, OSError, ValueError) as exc:
            print(f"FAIL {exc}\nEvidence: {args.out.resolve()}", file=sys.stderr)
            return 1
    print(f"All {len(result['checks'])} browser checks passed. Evidence: {args.out.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
