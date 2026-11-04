"""Cutouts: SAM's real mask bytes, the matte's clock and geometry, every mode, and one paid call."""

from __future__ import annotations

import http.client
import json
import struct
import subprocess
import sys
import threading
import types
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("meta_sam_parser")

from meta_sam_parser._mask_codec import _encode_raster

from vedit import matte, meta
from vedit.campaign import CampaignError, CampaignManager, capabilities
from vedit.compile import CUTOUT_MODES, _cutout_edge, _limited_yuv, compile_brief
from vedit.errors import CompileError
from vedit.ir import Brief, Canvas
from vedit.lint import subject_cutout_check
from vedit.media import _sample_aspect, probe, resolve_ffmpeg
from vedit.pipeline import execute
from vedit.render_plan import assembly_op
from vedit.studio.server import StudioHandler, _LoopbackServer
from vedit.timeline import plan_timeline

FIXTURE = json.loads(Path(__file__).with_name("fixtures").joinpath("sam_bowl.json").read_text(encoding="utf-8"))["records"]
KEYFRAME, TRACKED, ONE_BIT = FIXTURE["lossless_keyframe"], FIXTURE["lossless_tracked"], FIXTURE["one_bit_tracked"]


def png_of(width: int, height: int) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    rows = b"".join(b"\x00" + bytes((180, 170, 150)) * width for _ in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")


PNG = png_of(16, 9)


def token(record: dict[str, Any]) -> str:
    line = record["line"]
    return line[line.index("<|mask;"):]


def ffmpeg(*args: str) -> None:
    subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def raw_frames(path: Path, width: int, height: int) -> list[bytes]:
    """Every frame as gray bytes, scaled without any softening so edges stay measurable."""
    run = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", f"scale={width}:{height}:flags=neighbor,format=gray",
                          "-f", "rawvideo", "-"], check=True, capture_output=True)
    return [run.stdout[at:at + width * height] for at in range(0, len(run.stdout), width * height)]


def bright_box(frame: bytes, width: int, height: int, *, at_least: int = 128) -> tuple[int, int, int, int] | None:
    """The half-open box of the bright pixels, or None when there are none."""
    rows = [y for y in range(height) if max(frame[y * width:(y + 1) * width]) >= at_least]
    if not rows:
        return None
    columns = [x for x in range(width) if max(frame[rows[0] * width + x:rows[-1] * width + x + 1:width]) >= at_least]
    return columns[0], rows[0], columns[-1] + 1, rows[-1] + 1


def sam_line(frame: int, box: tuple[int, int, int, int], size: tuple[int, int] = (960, 540), *, ident: int = 0) -> str:
    """A real one-bit SAM record for a filled box. The wire's x2 and y2 are inclusive."""
    left, top, right, bottom = box
    columns, rows = right - left, bottom - top
    payload = _encode_raster(bytes([1]) * (columns * rows), columns, rows)
    return (f"<{frame}f>{ident}<|box;x1={left};y1={top};x2={right - 1};y2={bottom - 1};w={size[0]};h={size[1]};c=0.9|>"
            f"<|mask;x=0;y=0;c=0.9;data={rows},{columns},{payload}|>")


def clip_of(path: Path, *, size: str = "320x180", seconds: float = 1.0, square: int = 16, audio: bool = False) -> Path:
    """A white square crossing black, fast enough that one frame of slip moves it four pixels."""
    width, height = (int(part) for part in size.split("x"))
    args = ["-f", "lavfi", "-i", f"color=black:s={size}:r=30:d={seconds}", "-f", "lavfi", "-i", f"color=white:s={square}x{square}:r=30:d={seconds}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}"]
    args += ["-filter_complex", f"[0:v][1:v]overlay=x='{width // 8}+{width * 3 // 4 - width // 8}*t/{seconds}':y={(height - square) // 2}:format=auto,format=yuv420p[v]", "-map", "[v]"]
    if audio:
        args += ["-map", "2:a", "-c:a", "aac"]
    ffmpeg(*args, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "12", str(path))
    return path


def fixed_matte(path: Path, *, size: str = "160x90", box: str = "x=60:y=30:w=40:h=30", seconds: float = 1.0) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"color=black:s={size}:r=30:d={seconds}", "-vf", f"drawbox={box}:color=white:t=fill,format=gray", "-c:v", "ffv1", "-pix_fmt", "gray", str(path))
    return path


def proxy_of(clip: Path, assembly: dict[str, Any] | None, work: Path) -> Path:
    """The analysis copy, built exactly as ``CampaignManager._analysis_proxy`` builds it."""
    out = work / "analysis.mp4"
    ops: list[dict[str, Any]] = [{"op": "fit"}]
    if assembly:
        ops.append(assembly)
    ops.append({"op": "export", "preset": "web_preview", "path": str(out)})
    brief = Brief(name="analysis", sources={"footage": str(clip)}, ops=ops, canvas=Canvas(width=960, height=540, fps=30), hw=False, work_dir=str(work))
    execute(compile_brief(brief, work_dir=work).pipeline, progress=False)
    return out


def seed_sam(proxy: Path, text: str, work: Path, duration: float) -> dict[str, Any]:
    """Store a response as if it had been paid for, and read it back through the real index."""
    call = meta._sam_call(proxy, subject="the square", duration=duration, work_dir=work, budget_usd=1)
    meta._write_text(call["cache"].with_suffix(".sam"), text)
    return meta.subject_masks(proxy, subject="the square", duration=duration, work_dir=work, budget_usd=1)


class Http:
    """Meta's boundary. Each paid SAM call is counted, and it can answer in real SAM text."""

    def __init__(self, text: str = "", *, frames: int = 150) -> None:
        self.text, self.frames, self.calls = text, frames, []

    def __call__(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((method, path, kwargs))
        if method == "POST" and path == "/files":
            return {"id": "file-abc123", "status": "uploaded"}
        if method == "DELETE":
            return {"id": "file-abc123", "deleted": True}
        if method == "POST" and path == "/responses":
            return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": self.text}]}],
                    "usage": {"video_frames_processed": self.frames}}
        if method == "POST" and path == "/images/generations":
            import base64

            return {"data": [{"b64_json": base64.b64encode(PNG).decode()}], "output_format": "png"}
        pytest.fail(f"Unexpected provider request: {method} {path}")

    def paid(self, path: str = "/responses") -> int:
        return sum(method == "POST" and route == path for method, route, _ in self.calls)


@pytest.fixture
def enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODEL_API_KEY", "test-secret-never-write-to-disk")
    monkeypatch.setenv("VEDIT_META_ENABLED", "1")


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    return clip_of(tmp_path / "clip.mp4", seconds=2.0)


def ledger(work: Path) -> list[dict[str, Any]]:
    return json.loads((work / "meta-usage.json").read_text(encoding="utf-8"))["calls"]


# --- The decoder, on bytes the real API produced ---

def test_real_keyframe_is_hard_edged_and_its_outside_is_truly_zero() -> None:
    coverage, columns, rows = meta.decode_mask(token(KEYFRAME))
    assert (columns, rows) == (KEYFRAME["columns"], KEYFRAME["rows"])
    assert sum(1 for value in coverage if 8 <= value <= 247) < 50
    assert coverage.count(0) + coverage.count(255) > len(coverage) * 0.999
    assert sum(value >= 129 for value in coverage) == KEYFRAME["opaque_pixels"]


def test_real_one_bit_mask_is_all_or_nothing_and_agrees_with_lossless() -> None:
    bits, columns, rows = meta.decode_mask(token(ONE_BIT))
    assert set(bits) == {0, 255}
    assert bits.count(255) == ONE_BIT["opaque_pixels"]
    soft, *_ = meta.decode_mask(token(TRACKED))
    assert sum((a >= 129) != (b == 255) for a, b in zip(soft, bits)) < 0.001 * columns * rows


def test_only_the_public_decoder_is_used() -> None:
    # The SAM License bars reverse engineering, so nothing here reaches into the package's private modules.
    source = Path(meta.__file__).read_text(encoding="utf-8")
    assert "_mask_codec" not in source and "_decode_lossless_raster" not in source
    coverage, *_ = meta.decode_mask(token(TRACKED))
    assert set(coverage) == {0, 255}
    assert coverage.count(255) == TRACKED["opaque_pixels"]


def test_a_missing_decoder_says_how_to_install_it(monkeypatch: pytest.MonkeyPatch, enabled: None) -> None:
    monkeypatch.setitem(sys.modules, "meta_sam_parser", None)
    with pytest.raises(meta.MetaError, match="uv sync --extra sam"):
        meta.decode_mask(token(TRACKED))
    monkeypatch.setattr(meta, "find_spec", lambda name: None)
    assert meta.mask_decoder_ready() is False
    assert meta.capabilities()["features"]["cutout"] is False


@pytest.mark.parametrize("bad", ["<|mask;x=0;y=0|>", "<|mask;x=0;y=0;data=0,5,~abc|>", "<|mask;x=0;y=0;data=2,2,?abc|>", "<|mask;x=0;y=0;data=a,b,~abc|>",
                                 "<|mask;x=0;y=0;data=2,2,~abc|>", "<|mask;x=0;y=0;data=2,2,!abc|>"])
def test_a_corrupt_mask_is_a_clean_error(bad: str) -> None:
    with pytest.raises(meta.MetaError, match="unreadable mask"):
        meta.decode_mask(bad)


# --- One paid call ---

def test_masks_then_tracking_is_one_call_and_tracking_then_masks_is_too(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = "\n".join(sam_line(frame, (10, 20, 40, 60)) for frame in range(60))
    for order, first, second in ((1, meta.subject_masks, meta.track_subject), (2, meta.track_subject, meta.subject_masks)):
        http = Http(text, frames=60)
        monkeypatch.setattr(meta, "_request", http)
        work = tmp_path / f"order{order}"
        one = first(clip, subject="the square", duration=2.0, work_dir=work, budget_usd=0.05)
        two = second(clip, subject="the square", duration=2.0, work_dir=work, budget_usd=0.05)
        assert http.paid() == 1
        assert one["usage"]["cached"] is False and two["usage"]["cached"] is True and two["usage"]["charged_this_call_usd"] == 0
        calls = ledger(work)
        assert [(call["operation"], call["status"], call["usage"]["cost_usd"]) for call in calls] == [("tracking", "completed", 0.012)]


def test_a_refused_track_is_not_paid_for_twice_and_its_masks_still_work(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The subject leaves for a second: tracking refuses to invent motion, but the frames were billed.
    text = "\n".join(sam_line(frame, (10, 20, 40, 60)) for frame in range(60) if not 20 <= frame < 45)
    http = Http(text, frames=60)
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "work"
    for _ in range(2):
        with pytest.raises(meta.MetaError, match="hidden or missing"):
            meta.track_subject(clip, subject="the square", duration=2.0, work_dir=work, budget_usd=0.05)
    masks = meta.subject_masks(clip, subject="the square", duration=2.0, work_dir=work, budget_usd=0.05)
    assert http.paid() == 1
    assert len(masks["frames"]) == 35
    assert ledger(work)[0]["status"] == "completed"


def test_a_clip_with_no_subject_is_remembered_rather_than_asked_again(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http("", frames=60)
    monkeypatch.setattr(meta, "_request", http)
    for _ in range(2):
        masks = meta.subject_masks(clip, subject="a purple unicorn", duration=2.0, work_dir=tmp_path / "work", budget_usd=0.05)
        assert masks["frames"] == [] and masks["object_id"] is None and "subject" in masks["warning"].lower()
    assert http.paid() == 1


def test_masks_share_the_project_budget_before_any_upload(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    with pytest.raises(meta.MetaError, match="(?i)budget"):
        meta.subject_masks(clip, subject="the square", duration=2.0, work_dir=tmp_path / "work", budget_usd=0.001)
    assert http.calls == []


def test_tracking_asks_for_the_encoding_that_carries_alpha_and_streams_it(enabled: None, clip: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http("\n".join(sam_line(frame, (10, 20, 40, 60)) for frame in range(60)), frames=60)
    monkeypatch.setattr(meta, "_request", http)
    meta.track_subject(clip, subject="the square", duration=2.0, work_dir=tmp_path / "work", budget_usd=0.05)
    sent = next(kwargs for method, path, kwargs in http.calls if path == "/responses")
    assert sent["stream"] is True and sent["payload"]["stream"] is True
    assert sent["payload"]["metadata"] == {"mask_encoding": "lossless", "include_confidence": "true"}


def test_the_index_follows_one_object_and_places_each_mask_in_its_box(enabled: None, clip: Path, tmp_path: Path) -> None:
    # Frames arrive out of order, skip, and repeat; object 7 is on screen longest. The later record of a frame wins.
    size = (320, 180)
    lines = [sam_line(4, (5, 6, 9, 10), size, ident=3) + "," + sam_line(4, (20, 20, 40, 44), size, ident=7).split("f>", 1)[1],
             sam_line(2, (19, 21, 39, 45), size, ident=7), sam_line(1, (18, 20, 38, 44), size, ident=7), sam_line(2, (22, 22, 42, 46), size, ident=7)]
    masks = seed_sam(clip, "\n".join(lines), tmp_path / "work", 2.0)
    assert masks["object_id"] == 7
    assert (masks["frame_width"], masks["frame_height"]) == size
    assert [entry["frame"] for entry in masks["frames"]] == [1, 2, 4]
    assert masks["frames"][1]["box"] == (22, 22, 42, 46)
    assert masks["frames"][0]["time"] == pytest.approx(1 / 30, abs=1e-6)
    raster, columns, rows = meta.read_mask(masks["raw"], masks["frames"][2]["span"])
    assert (columns, rows) == (20, 24) and set(raster) == {255}
    assert meta._BOX.match("0<|box;x1=1;y1=2;x2=3;y2=4;w=9;h=9;c=9.5e-05|>") is not None


# --- The stream, over a real socket ---

class Stream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    chunks: list[bytes] = []
    seen: list[tuple[str, str, Any]] = []

    def log_message(self, *args: Any) -> None:
        pass

    def _json(self, value: dict[str, Any]) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_DELETE(self) -> None:
        self._json({"id": "file-abc123", "deleted": True})

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        if self.path == "/v1/files":
            self._json({"id": "file-abc123"})
            return
        self.seen.append((self.path, self.headers.get("Accept", ""), json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        for chunk in self.chunks:
            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            self.wfile.flush()
        self.wfile.write(b"0\r\n\r\n")
        self.close_connection = True


def event(kind: str, **data: Any) -> bytes:
    return f"event: {kind}\ndata: {json.dumps({'type': kind, **data})}\n\n".encode()


def sam_stream(lines: list[str], *, frames: int, closing: bool = True, repeats: bool = True) -> list[bytes]:
    """The event order the live API sends, captured on 2026-10-31. Its repeats here are unparseable on purpose."""
    text = "\n".join(lines) + "\n"
    chunks = [event("response.created", response={"status": "in_progress", "output": []}),
              event("response.output_item.added", item={"type": "message"}), event("response.content_part.added", part={"text": ""})]
    chunks += [event("response.output_text.delta", delta=line + "\n") for line in lines]
    if repeats:
        chunks += [f"event: {kind}\ndata: {{not json\n\n".encode() for kind in ("response.output_text.done", "response.content_part.done", "response.output_item.done")]
    if closing:
        chunks.append(event("response.completed", response={"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
                                                            "usage": {"input_tokens": 0, "output_tokens": 0, "video_frames_processed": frames}}))
        chunks.append(b"data: [DONE]\n\n")
    return chunks


@pytest.fixture
def stream_server(monkeypatch: pytest.MonkeyPatch, enabled: None):
    Stream.seen = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), Stream)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(meta, "_BASE", f"http://127.0.0.1:{server.server_address[1]}/v1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    try:
        yield Stream
    finally:
        server.shutdown()
        server.server_close()


def test_a_real_chunked_stream_becomes_tracking_masks_and_one_ledger_line(stream_server: type[Stream], clip: Path, tmp_path: Path) -> None:
    lines = [TRACKED["line"].replace("<10f>", f"<{frame}f>") for frame in range(60)]
    stream_server.chunks = sam_stream(lines, frames=60)
    work = tmp_path / "work"
    track = meta.track_subject(clip, subject="the bowl", duration=2.0, work_dir=work, budget_usd=0.05)
    assert len(stream_server.seen) == 1
    path, accept, body = stream_server.seen[0]
    assert (path, accept, body["stream"], body["model"]) == ("/v1/responses", "text/event-stream", True, "sam-3.1")
    assert track["usage"]["video_frames_processed"] == 60 and track["usage"]["cost_usd"] == pytest.approx(0.012)
    assert track["points"][0]["x"] == pytest.approx(234 / 960)
    masks = meta.subject_masks(clip, subject="the bowl", duration=2.0, work_dir=work, budget_usd=0.05)
    assert len(stream_server.seen) == 1 and len(masks["frames"]) == 60
    coverage, *_ = meta.read_mask(masks["raw"], masks["frames"][59]["span"])
    assert sum(value >= 129 for value in coverage) == TRACKED["opaque_pixels"]
    assert (work / "meta-cache").is_dir() and "test-secret" not in Path(masks["raw"]).read_text(encoding="latin-1")


def test_zero_matches_completes_with_no_text(stream_server: type[Stream], clip: Path, tmp_path: Path) -> None:
    stream_server.chunks = sam_stream([], frames=60, repeats=False)
    stream_server.chunks = [chunk for chunk in stream_server.chunks if b"output_text.delta" not in chunk]
    masks = meta.subject_masks(clip, subject="the bowl", duration=2.0, work_dir=tmp_path / "work", budget_usd=0.05)
    assert masks["frames"] == [] and masks["usage"]["cost_usd"] == pytest.approx(0.012)


@pytest.mark.parametrize("chunks, words", [
    (lambda: sam_stream([TRACKED["line"]], frames=60, closing=False), "ended early"),
    (lambda: [event("response.output_text.delta", delta="x"), event("error", message="secret upstream detail")], "stopped partway"),
    (lambda: [event("response.failed", response={"status": "failed", "error": {"message": "secret upstream detail"}})], "did not finish"),
    (lambda: [event("response.incomplete", response={"status": "incomplete", "incomplete_details": {"reason": "content_filter"}})], "did not finish"),
])
def test_a_broken_stream_keeps_its_reservation_and_nothing_is_stored(stream_server: type[Stream], clip: Path, tmp_path: Path, chunks: Any, words: str) -> None:
    stream_server.chunks = chunks()
    work = tmp_path / "work"
    with pytest.raises(meta.MetaError, match=words) as caught:
        meta.subject_masks(clip, subject="the bowl", duration=2.0, work_dir=work, budget_usd=0.05)
    assert "secret" not in str(caught.value)
    calls = ledger(work)
    assert len(calls) == 1 and calls[0]["status"] == "failed_or_incomplete" and calls[0]["reserved_usd"] > 0
    assert not list((work / "meta-cache").glob("*.sam")) if (work / "meta-cache").exists() else True


# --- Geometry and the clock ---

@pytest.mark.parametrize("source, expected", [((1280, 720), (0, 0, 960, 540)), ((1920, 1080), (0, 0, 960, 540)), ((720, 720), (210, 0, 540, 540)),
                                              ((1080, 1920), (328, 0, 304, 540)), ((1920, 800), (0, 70, 960, 400)), ((640, 480), (120, 0, 720, 540))])
def test_picture_area_matches_where_ffmpeg_really_puts_the_picture(tmp_path: Path, source: tuple[int, int], expected: tuple[int, int, int, int]) -> None:
    area = matte.picture_area(*source, 960, 540)
    assert (area.left, area.top, area.width, area.height) == expected
    white = tmp_path / "white.mp4"
    ffmpeg("-f", "lavfi", "-i", f"color=white:s={source[0]}x{source[1]}:r=30:d=0.2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(white))
    proxy = proxy_of(white, None, tmp_path / "proxy")
    box = bright_box(raw_frames(proxy, 960, 540)[0], 960, 540)
    assert box == (area.left, area.top, area.left + area.width, area.top + area.height)


@pytest.mark.parametrize("value, ratio", [("16:11", 16 / 11), ("1:1", 1.0), ("0:1", 1.0), ("N/A", 1.0), ("", 1.0), (None, 1.0), ("4:0", 1.0)])
def test_a_file_that_does_not_state_its_pixel_shape_is_square(value: Any, ratio: float) -> None:
    assert _sample_aspect(value) == pytest.approx(ratio)


@pytest.mark.render
def test_wide_pixels_stay_wide_through_assembly_and_the_matte_still_finds_the_picture(enabled: None, tmp_path: Path) -> None:
    """PAL 16:9 stores wide pixels: a 110x160 box in the file is a square on screen."""
    source = tmp_path / "pal.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=white:s=720x576:r=25:d=1", "-vf", "setsar=16/11", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(source))
    info = probe(source)
    assert info.video.sample_aspect_ratio == pytest.approx(16 / 11)
    assert (info.square_width, info.display_height) == (1047, 576)
    area = matte.picture_area(info.square_width, info.display_height, 960, 540)
    proxy = proxy_of(source, None, tmp_path / "white")
    assert bright_box(raw_frames(proxy, 960, 540)[0], 960, 540) == (area.left, area.top, area.left + area.width, area.top + area.height)

    boxed = tmp_path / "boxed.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=c=black:s=720x576:r=25:d=1", "-vf", "drawbox=x=305:y=208:w=110:h=160:color=white:t=fill,setsar=16/11",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(boxed))
    assembly = {"op": "select_ranges", "ranges": [{"start": 0.0, "end": 0.8}]}
    proxy = proxy_of(boxed, assembly, tmp_path / "proxy")
    lines = [sam_line(index, box) for index, frame in enumerate(raw_frames(proxy, 960, 540)) if (box := bright_box(frame, 960, 540))]
    masks = seed_sam(proxy, "\n".join(lines), tmp_path / "meta", probe(proxy).duration)
    out = tmp_path / "matte.mkv"
    matte.build_matte(masks, path=out, area=area, frames=24, filters="null")
    ops = [assembly, {"op": "cutout", "matte": str(out), "mode": "color", "color": "#000000"}, {"op": "export", "preset": "web_preview", "path": str(tmp_path / "done.mp4")}]
    brief = Brief(name="check", sources={"footage": str(boxed)}, ops=ops, canvas=Canvas(width=720, height=576, fps=30), hw=False, work_dir=str(tmp_path / "render"))
    execute(compile_brief(brief, work_dir=tmp_path / "render").pipeline, progress=False)
    tape = next((tmp_path / "render").glob("*select_ranges.mp4"))
    assert (probe(tape).width, probe(tape).height) == (1046, 576)
    width, height = probe(tape).width, probe(tape).height
    picture = raw_frames(tape, width, height)[8]
    composite = raw_frames(next((tmp_path / "render").glob("*cutout.mp4")), width, height)[8]
    left, top, right, bottom = bright_box(picture, width, height)
    assert abs((right - left) - (bottom - top)) <= 2, "the box should be square on screen"
    assert all(abs(a - b) <= 2 for a, b in zip((left, top, right, bottom), bright_box(composite, width, height)))


def test_the_edge_keeps_its_width_as_the_tape_outgrows_the_matte(tmp_path: Path) -> None:
    cut = fixed_matte(tmp_path / "matte.mkv", size="960x540", box="x=10:y=10:w=20:h=20", seconds=0.2)
    assert _cutout_edge((1280, 720), cut) == "lut=y='clip((val-96)*255/143,0,255)'"
    assert _cutout_edge(None, cut) == _cutout_edge((640, 360), cut) == _cutout_edge((1280, 720), cut)
    assert _cutout_edge((1920, 1080), cut) == "lut=y='clip((val-120)*255/95,0,255)'"
    assert _cutout_edge((3840, 2160), cut) == "lut=y='clip((val-143)*255/48,0,255)'"
    assert _cutout_edge((15360, 8640), cut) == "lut=y='clip((val-151)*255/32,0,255)'"


def test_nearest_frames_fill_a_short_gap_but_never_cross_a_cut_or_reach_too_far() -> None:
    assert matte.nearest_frames([0, 1, 2, 10, 11], 14, cuts=[6], reach=3) == [0, 1, 2, 2, 2, 2, None, 10, 10, 10, 10, 11, 11, 11]
    assert matte.nearest_frames([5], 8, reach=2) == [None, None, None, 5, 5, 5, 5, 5][:3] + [5, 5, 5, 5, 5]
    assert matte.nearest_frames([], 3, reach=9) == [None, None, None]
    assert matte.nearest_frames([1, 3], 5, reach=1) == [1, 1, 1, 3, 3]


def test_paste_places_a_box_local_mask_and_clips_it_to_the_picture() -> None:
    area = matte.Area(left=2, top=1, width=4, height=3)
    raster = bytes(range(1, 7))
    canvas = matte.paste(raster, 3, 2, (3, 2, 6, 4), area)
    assert bytes(canvas) == bytes([0, 0, 0, 0,   0, 1, 2, 3,   0, 4, 5, 6])
    clipped = matte.paste(raster, 3, 2, (0, 0, 3, 2), area)  # only its last column of its last row survives
    assert bytes(clipped) == bytes([6, 0, 0, 0] + [0] * 8)
    assert bytes(matte.paste(raster, 3, 2, (40, 40, 43, 42), area)) == bytes(12)
    # A mask smaller than its box is stretched into it, not stuck in a corner.
    assert bytes(matte.paste(bytes([9]), 1, 1, (2, 1, 6, 4), area)) == bytes([9]) * 12


def test_flicker_ignores_cuts_and_coverage_counts_only_real_masks() -> None:
    steady = [1000 + (index % 2) for index in range(40)]
    assert matte.flicker(steady) < 0.002
    jumpy = [1000, 400] * 20
    assert matte.flicker(jumpy) == pytest.approx(0.6)
    shots = [1000] * 20 + [100] * 20
    assert matte.flicker(shots, [20]) == 0
    stats = matte.report([100, 100, 0, 100], masked=2, filled=1, picture=1000, fps=30)
    assert (stats["coverage"], stats["filled_frames"], stats["empty_frames"], stats["first_missing"]) == (0.5, 1, 1, 0.067)


@pytest.mark.render
@pytest.mark.parametrize("size, plan", [
    ("320x180", {"segments": [{"start": 0, "end": 1.0}]}),
    ("180x320", {"segments": [{"start": 0, "end": 1.0}]}),
    ("320x180", {"segments": [{"start": 0.6, "end": 1.4}, {"start": 0.1, "end": 0.5}]}),
    ("320x180", {"segments": [{"start": 0.2, "end": 1.0, "speed": 2.0}, {"start": 1.2, "end": 1.8, "speed": 0.5}]}),
    ("320x180", {"segments": [{"start": 0, "end": 1.6}], "edit": {"removed": [[0.5, 0.9]]}}),
], ids=["plain", "letterboxed", "reordered-cut", "speed", "removed"])
def test_matte_lands_on_the_assembled_picture_frame_by_frame(enabled: None, tmp_path: Path, size: str, plan: dict[str, Any]) -> None:
    """A subject at a known place on every frame: what SAM would mark is what the finished clip shows."""
    source = clip_of(tmp_path / "source.mp4", size=size, seconds=2.0)
    view = plan_timeline(plan)
    assembly = assembly_op(view, plan.get("edit") or {})
    proxy = proxy_of(source, assembly, tmp_path / "proxy")
    width, height = probe(source).width, probe(source).height
    area = matte.picture_area(width, height, 960, 540)
    # Stand in for SAM by reading the proxy it would read.
    lines = []
    for index, frame in enumerate(raw_frames(proxy, 960, 540)):
        box = bright_box(frame, 960, 540)
        if box:
            lines.append(sam_line(index, box))
    fps = probe(proxy).fps
    masks = seed_sam(proxy, "\n".join(lines), tmp_path / "meta", probe(proxy).duration)
    frames, cuts = matte.finished_frames(view, fps)
    out = tmp_path / "matte.mkv"
    stats = matte.build_matte(masks, path=out, area=area, frames=frames, cuts=cuts)
    assert stats["frames"] == frames and stats["coverage"] == 1.0
    assert (probe(out).width, probe(out).height) == (area.width, area.height)
    ops = [assembly, {"op": "cutout", "matte": str(out), "mode": "color", "color": "#000000"}, {"op": "export", "preset": "web_preview", "path": str(tmp_path / "done.mp4")}]
    brief = Brief(name="check", sources={"footage": str(source)}, ops=ops, canvas=Canvas(width=width, height=height, fps=30), hw=False, work_dir=str(tmp_path / "render"))
    compiler = compile_brief(brief, work_dir=tmp_path / "render")
    execute(compiler.pipeline, progress=False)
    pictures = raw_frames(next((tmp_path / "render").glob("*select_ranges.mp4")), width, height)
    alphas = raw_frames(out, width, height)
    composites = raw_frames(next((tmp_path / "render").glob("*cutout.mp4")), width, height)
    assert len(pictures) == len(alphas) == len(composites) == frames
    matte_error = composite_error = 0
    for index, (picture, alpha, composite) in enumerate(zip(pictures, alphas, composites)):
        seen, marked, kept = bright_box(picture, width, height), bright_box(alpha, width, height), bright_box(composite, width, height)
        assert seen and marked and kept, f"frame {index} lost the subject"
        matte_error = max(matte_error, *(abs(a - b) for a, b in zip(seen, marked)))
        composite_error = max(composite_error, *(abs(a - b) for a, b in zip(seen, kept)))
    # One frame of slip would move these boxes by four pixels. What survives in the
    # composite is the proof, because that is where the op's own frame sync acts.
    print(f"alignment {plan.get('segments')}: matte {matte_error}px, composite {composite_error}px over {frames} frames")
    assert matte_error <= 2
    assert composite_error <= 2


@pytest.mark.render
def test_a_transition_keeps_the_matte_and_the_tape_the_same_length(enabled: None, tmp_path: Path) -> None:
    """Overlapping windows are where frame accounting slips. SAM reads the dissolve itself."""
    source = clip_of(tmp_path / "source.mp4", seconds=2.0)
    plan = {"segments": [{"start": 0.0, "end": 0.8}, {"start": 1.0, "end": 1.8}], "edit": {"transition": {"type": "fade", "seconds": 0.3}}}
    view = plan_timeline(plan)
    assert view["transition_seconds"] == pytest.approx(0.3)
    assembly = assembly_op(view, plan["edit"])
    proxy = proxy_of(source, assembly, tmp_path / "proxy")
    frames, cuts = matte.finished_frames(view, 30.0)
    assert frames == round(1.3 * 30) and cuts == [15]
    assert len(raw_frames(proxy, 96, 54)) == frames
    lines = [sam_line(index, box) for index, frame in enumerate(raw_frames(proxy, 960, 540)) if (box := bright_box(frame, 960, 540, at_least=48))]
    masks = seed_sam(proxy, "\n".join(lines), tmp_path / "meta", probe(proxy).duration)
    out = tmp_path / "matte.mkv"
    stats = matte.build_matte(masks, path=out, area=matte.picture_area(320, 180, 960, 540), frames=frames, cuts=cuts)
    assert stats["coverage"] == 1.0
    ops = [assembly, {"op": "cutout", "matte": str(out), "mode": "mono"}, {"op": "export", "preset": "web_preview", "path": str(tmp_path / "done.mp4")}]
    brief = Brief(name="check", sources={"footage": str(source)}, ops=ops, canvas=Canvas(width=320, height=180, fps=30), hw=False, work_dir=str(tmp_path / "render"))
    execute(compile_brief(brief, work_dir=tmp_path / "render").pipeline, progress=False)
    assert len(raw_frames(next((tmp_path / "render").glob("*cutout.mp4")), 32, 18)) == frames


@pytest.mark.render
def test_a_lost_subject_empties_the_matte_only_after_the_short_gap(enabled: None, tmp_path: Path) -> None:
    source = clip_of(tmp_path / "source.mp4", seconds=2.0)
    proxy = proxy_of(source, None, tmp_path / "proxy")
    # Gone from frame 20 to the end: half a second of held mask, then nothing.
    text = "\n".join(sam_line(frame, (300, 200, 400, 300)) for frame in range(20))
    masks = seed_sam(proxy, text, tmp_path / "meta", probe(proxy).duration)
    out = tmp_path / "matte.mkv"
    stats = matte.build_matte(masks, path=out, area=matte.Area(0, 0, 960, 540), frames=60, filters="null")
    assert (stats["masked_frames"], stats["filled_frames"], stats["empty_frames"]) == (20, 15, 25)
    assert stats["first_missing"] == pytest.approx(35 / 30, abs=0.002)
    alphas = raw_frames(out, 96, 54)
    assert max(alphas[34]) == 255 and max(alphas[35]) == 0


@pytest.mark.render
def test_cancelling_a_matte_stops_ffmpeg_and_leaves_no_file(enabled: None, tmp_path: Path) -> None:
    source = clip_of(tmp_path / "source.mp4", seconds=1.0)
    proxy = proxy_of(source, None, tmp_path / "proxy")
    masks = seed_sam(proxy, "\n".join(sam_line(frame, (10, 10, 50, 50)) for frame in range(30)), tmp_path / "meta", probe(proxy).duration)
    cancel = threading.Event()
    cancel.set()
    out = tmp_path / "matte.mkv"
    with pytest.raises(matte.RenderCancelled):
        matte.build_matte(masks, path=out, area=matte.Area(0, 0, 960, 540), frames=30, cancel_event=cancel)
    assert not out.exists() and not list(tmp_path.glob("matte.part*"))


def test_a_matte_with_no_masks_is_refused_before_ffmpeg_starts(tmp_path: Path) -> None:
    with pytest.raises(matte.MatteError, match="no subject masks"):
        matte.build_matte({"frames": [], "fps": 30.0}, path=tmp_path / "m.mkv", area=matte.Area(0, 0, 10, 10), frames=5)


# --- The compile op ---

def render(tmp_path: Path, name: str, source: Path, *ops: dict[str, Any], **sources: str) -> Path:
    work = tmp_path / name
    final = work / "video.mp4"
    info = probe(source)
    brief = Brief(name="cutout", sources={"footage": str(source), **sources}, ops=[*ops, {"op": "export", "preset": "web_preview", "path": str(final)}],
                  canvas=Canvas(width=info.width, height=info.height, fps=30), hw=False, work_dir=str(work))
    execute(compile_brief(brief, work_dir=work).pipeline, progress=False)
    return next(work.glob("*cutout.mp4"))


def rgb_at(path: Path, x: int, y: int, *, frame: int = 10) -> tuple[int, int, int]:
    run = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", f"select=eq(n\\,{frame}),scale=flags=accurate_rnd+full_chroma_int,format=rgb24,crop=1:1:{x}:{y}",
                          "-frames:v", "1", "-f", "rawvideo", "-"], check=True, capture_output=True)
    return tuple(run.stdout[:3])


@pytest.fixture
def scene(tmp_path: Path) -> tuple[Path, Path]:
    """A teal field with a red block where the matte is open, and a tone, so sound must survive too."""
    path = tmp_path / "scene.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=0x208080:s=320x180:r=30:d=1", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
           "-vf", "drawbox=x=120:y=60:w=80:h=60:color=0xD03020:t=fill,format=yuv420p", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "6", "-c:a", "aac", "-shortest", str(path))
    return path, fixed_matte(tmp_path / "matte.mkv")


@pytest.mark.render
@pytest.mark.parametrize("mode", CUTOUT_MODES)
def test_every_mode_keeps_the_subject_changes_the_rest_and_keeps_the_sound(tmp_path: Path, scene: tuple[Path, Path], mode: str) -> None:
    source, cut = scene
    still = tmp_path / "still.png"
    ffmpeg("-f", "lavfi", "-i", "color=0xC89030:s=500x100", "-frames:v", "1", str(still))
    op: dict[str, Any] = {"op": "cutout", "matte": str(cut), "mode": mode, "color": "#10A040", "blur": 0.6}
    if mode == "image":
        op["image"] = "backdrop"
    out = render(tmp_path, mode, source, op, **({"backdrop": str(still)} if mode == "image" else {}))
    info = probe(out)
    assert (info.width, info.height, info.has_audio, info.has_video) == (320, 180, True, True)
    assert len(raw_frames(out, 32, 18)) == 30
    original, inside = rgb_at(source, 160, 90), rgb_at(out, 160, 90)
    assert all(abs(a - b) <= 8 for a, b in zip(original, inside)), f"{mode} changed the subject"
    field, outside = rgb_at(source, 20, 20), rgb_at(out, 20, 20)
    if mode == "mono":
        assert max(outside) - min(outside) <= 3
    elif mode == "dim":
        assert sum(outside) < 0.6 * sum(field)
    elif mode == "color":
        # Read from the delivered file: only it is tagged, so only it decodes without guessing the matrix.
        assert all(abs(a - b) <= 2 for a, b in zip(rgb_at(out.parent / "video.mp4", 20, 20), (0x10, 0xA0, 0x40)))
    elif mode == "image":
        assert all(abs(a - b) <= 2 for a, b in zip(rgb_at(out.parent / "video.mp4", 20, 20), (0xC8, 0x90, 0x30)))
    else:
        assert all(abs(a - b) <= 8 for a, b in zip(outside, field))  # an even field blurs to itself


@pytest.mark.render
def test_a_blurred_background_does_not_glow_with_the_subjects_color(tmp_path: Path) -> None:
    source = tmp_path / "bright.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=0x101010:s=320x180:r=30:d=1", "-vf", "drawbox=x=120:y=60:w=80:h=60:color=white:t=fill,format=yuv420p",
           "-c:v", "libx264", "-preset", "ultrafast", "-crf", "6", str(source))
    cut = fixed_matte(tmp_path / "matte.mkv")
    out = render(tmp_path, "blur", source, {"op": "cutout", "matte": str(cut), "mode": "blur", "blur": 1.0})
    # Three pixels outside a white subject on a near-black field. A plain blur of the frame leaves this gray.
    assert max(rgb_at(out, 116, 90)) < 48
    assert max(rgb_at(out, 160, 56)) < 48


@pytest.mark.render
@pytest.mark.parametrize("seconds", [0.5, 1.5], ids=["short-matte", "long-matte"])
def test_the_clip_decides_the_length_whatever_the_matte_does(tmp_path: Path, scene: tuple[Path, Path], seconds: float) -> None:
    source, _ = scene
    cut = fixed_matte(tmp_path / "other.mkv", seconds=seconds)
    out = render(tmp_path, "length", source, {"op": "cutout", "matte": str(cut), "mode": "color", "color": "#000000"})
    assert len(raw_frames(out, 32, 18)) == 30


@pytest.mark.render
def test_solid_color_survives_the_trip_to_the_finished_bt709_file(tmp_path: Path, scene: tuple[Path, Path]) -> None:
    source, cut = scene
    out = render(tmp_path, "solid", source, {"op": "cutout", "matte": str(cut), "mode": "color", "color": "#E6E0D2"})
    final = out.parent / "video.mp4"
    stream = probe(final).video
    assert (stream.color_space, stream.color_range) == ("bt709", "tv")
    assert all(abs(a - b) <= 2 for a, b in zip(rgb_at(final, 20, 20), (0xE6, 0xE0, 0xD2)))


def test_bt709_endpoints_and_a_primary_are_exact() -> None:
    assert _limited_yuv("#000000") == (16, 128, 128)
    assert _limited_yuv("#FFFFFF") == (235, 128, 128)
    assert _limited_yuv("#FF0000") == (63, 102, 240)


@pytest.mark.render
@pytest.mark.parametrize("op, words", [
    ({"mode": "sparkles"}, "cutout mode"),
    ({"mode": "none"}, "cutout mode"),
    ({"mode": "blur", "blur": 1.5}, "between 0 and 1"),
    ({"mode": "color", "color": "red; rm -rf"}, "color must look like"),
    ({"mode": "image"}, "requires an image"),
])
def test_a_bad_cutout_op_fails_at_compile_time(tmp_path: Path, scene: tuple[Path, Path], op: dict[str, Any], words: str) -> None:
    source, cut = scene
    brief = Brief(name="bad", sources={"footage": str(source)}, ops=[{"op": "cutout", "matte": str(cut), **op}], canvas=Canvas(width=320, height=180, fps=30), hw=False, work_dir=str(tmp_path / "w"))
    with pytest.raises(CompileError, match=words):
        compile_brief(brief, work_dir=tmp_path / "w")


def test_a_cutout_without_a_matte_fails_at_compile_time(tmp_path: Path, scene: tuple[Path, Path]) -> None:
    brief = Brief(name="bad", sources={"footage": str(scene[0])}, ops=[{"op": "cutout", "mode": "blur"}], canvas=Canvas(width=320, height=180, fps=30), hw=False, work_dir=str(tmp_path / "w"))
    with pytest.raises(CompileError, match="requires a matte"):
        compile_brief(brief, work_dir=tmp_path / "w")


# --- The receipt ---

def test_the_receipt_passes_a_clean_cutout_and_explains_a_poor_one() -> None:
    good = subject_cutout_check({"frames": 300, "coverage": 0.99, "flicker": 0.012, "filled_frames": 3, "empty_frames": 0, "first_missing": None})
    assert good["name"] == "auto: subject cutout covers the subject"
    assert good["passed"] is True and "99.0% of 300" in good["note"] and "3 held" in good["note"]
    sparse = subject_cutout_check({"frames": 300, "coverage": 0.5, "flicker": 0.01, "filled_frames": 0, "empty_frames": 150, "first_missing": 4.0})
    assert sparse["passed"] is False and sparse["at"] == 4.0 and "150 with no subject" in sparse["note"]
    assert subject_cutout_check({"frames": 300, "coverage": 1.0, "flicker": 0.2})["passed"] is False
    assert subject_cutout_check(None) == {"name": "auto: subject cutout covers the subject", "passed": True, "at": None, "note": "not requested", "basis": "derived", "by": "lint"}


# --- The whole render, through the campaign ---

@pytest.fixture
def manager(tmp_path: Path):
    instance = CampaignManager(tmp_path, start_worker=False)
    try:
        yield instance
    finally:
        instance.close()


def footage(tmp_path: Path) -> Path:
    path = tmp_path / "vedit-sessions" / "source-test" / "inbox" / "clip.mp4"
    path.parent.mkdir(parents=True)
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=320x180:r=30:d=1.6", "-f", "lavfi", "-i", "sine=frequency=330:duration=1.6",
           "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path))
    return path


def planned_project(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **request: Any) -> tuple[str, dict[str, Any]]:
    def fake_plan(proxy: Path, **kwargs: Any) -> dict[str, Any]:
        return {"title": "Cut out", "summary": "A plan.", "caption": "", "cover_prompt": "", "hashtags": [], "provider": "meta", "usage": {},
                "segments": [{"start": 0.1, "end": 1.1, "label": "Moment", "reason": "Visible."}]}

    monkeypatch.setattr(meta, "plan_story", fake_plan)
    job = manager.create({"paths": [str(footage(tmp_path))], "goal": "Show the product clearly", "target_duration": 1, "captions": False, "aspect": "16:9", **request})
    manager.process(job["id"])
    planned = manager.get(job["id"])
    assert planned["status"] == "planned", planned["error"]
    return job["id"], planned["plan"]


def with_cutout(plan: dict[str, Any], **cutout: Any) -> dict[str, Any]:
    return dict(plan, edit=dict(plan["edit"], cutout=dict(plan["edit"]["cutout"], **cutout)))


def sam_for_the_proxy(http: Http) -> None:
    """Answer SAM's question with a real line for every frame of whatever was uploaded."""
    original = http.__call__

    def answer(method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if method == "POST" and path == "/files":
            info = probe(kwargs["body"].source)
            http.frames = round(info.duration * info.fps)
            line = sam_line(0, (300, 150, 660, 400))
            http.text = "\n".join(line.replace("<0f>", f"<{frame}f>", 1) for frame in range(http.frames))
        return Http.__call__(http, method, path, **kwargs)

    http.answer = answer  # type: ignore[attr-defined]
    del original


@pytest.mark.render
def test_a_campaign_render_cuts_out_tracks_and_bills_one_sam_call(enabled: None, manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident, plan = planned_project(manager, tmp_path, monkeypatch, provider="meta", subject="the wooden bowl")
    http = Http()
    sam_for_the_proxy(http)
    monkeypatch.setattr(meta, "_request", http.answer)  # type: ignore[attr-defined]
    # The same words in other capitals are the same subject, so they must not buy a second call.
    manager.render(ident, {"plan": with_cutout(plan, mode="color", subject="The Wooden Bowl", color="#E6E0D2")})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    assert http.paid() == 1
    report = json.loads(manager.artifact(ident, "report").read_text(encoding="utf-8"))
    assert report["cutout"]["coverage"] == 1.0 and report["cutout"]["frames"] == 30
    check = next(item for item in done["result"]["checks"] if item["name"] == "auto: subject cutout covers the subject")
    assert check["passed"] is True
    order = [op["op"] for op in report["project"]["ops"]] if "project" in report else None
    brief = manager.artifact(ident, "brief").read_text(encoding="utf-8")
    assert brief.index("select_ranges") < brief.index("cutout") < brief.index("track_reframe")
    assert order is None or order.index("cutout") == order.index("select_ranges") + 1
    tracking = [call for call in ledger(manager.root / ident / "meta") if call["operation"] == "tracking"]
    assert len(tracking) == 1 and tracking[0]["usage"]["video_frames_processed"] == http.frames
    # A second render of the same plan reuses the matte and pays for nothing.
    manager.render(ident, {"plan": with_cutout(plan, mode="dim", subject="the wooden bowl")})
    manager.process(ident)
    assert manager.get(ident)["status"] == "complete"
    assert http.paid() == 1
    assert len(list((manager.root / ident / "matte").glob("*.mkv"))) == 1


@pytest.mark.render
def test_a_cutout_needs_no_tracking_subject_and_works_with_following_off(enabled: None, manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident, plan = planned_project(manager, tmp_path, monkeypatch, provider="meta")
    http = Http()
    sam_for_the_proxy(http)
    monkeypatch.setattr(meta, "_request", http.answer)  # type: ignore[attr-defined]
    manager.render(ident, {"plan": with_cutout(plan, mode="mono", subject="the mug"), "tracking": False})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    report = json.loads(manager.artifact(ident, "report").read_text(encoding="utf-8"))
    assert report["cutout"]["coverage"] == 1.0
    assert "cutout" in manager.artifact(ident, "brief").read_text(encoding="utf-8")


@pytest.mark.render
def test_image_cutout_filter_graph_does_not_use_deprecated_scale2ref(enabled: None, manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident, plan = planned_project(manager, tmp_path, monkeypatch, provider="meta", subject="the wooden bowl")
    image = manager.sessions / "source-test" / "inbox" / "background.png"
    image.write_bytes(PNG)
    asset = manager.add_asset(ident, {"path": str(image)})
    http = Http()
    sam_for_the_proxy(http)
    monkeypatch.setattr(meta, "_request", http.answer)  # type: ignore[attr-defined]
    chosen = with_cutout(plan, mode="image", subject="the wooden bowl", asset=asset["id"])
    manager.render(ident, {"plan": chosen})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    source = manager.sessions / "source-test" / "inbox" / "clip.mp4"
    matte_path = fixed_matte(tmp_path / "test-matte.mkv", size="320x180", seconds=1.6)
    compile_dir = tmp_path / "compile-check"
    compile_brief(Brief(name="scale-check", sources={"footage": str(source), "matte": str(matte_path), "background": str(image)},
                        ops=[{"op": "cutout", "matte": "matte", "mode": "image", "image": "background", "color": "#101418", "blur": 0.6},
                             {"op": "export", "preset": "youtube_1080p", "path": str(tmp_path / "compiled.mp4")}],
                        canvas=Canvas(width=320, height=180, fps=30), hw=False, work_dir=str(compile_dir)), work_dir=compile_dir)
    graphs = list(compile_dir.rglob("*.filter.txt"))
    assert len(graphs) == 1
    graph = graphs[0].read_text(encoding="utf-8")
    assert "scale2ref" not in graph
    assert "[raw]scale=" in graph and "[still]scale=w='ceil(max(" in graph


@pytest.mark.render
def test_a_missing_subject_offers_recovery_and_taking_it_renders_without_paying_again(enabled: None, manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident, plan = planned_project(manager, tmp_path, monkeypatch, provider="meta")
    http = Http("", frames=30)
    monkeypatch.setattr(meta, "_request", http)
    chosen = with_cutout(plan, mode="blur", subject="purple unicorn")
    manager.render(ident, {"plan": chosen})
    manager.process(ident)
    failed = manager.get(ident)
    assert failed["status"] == "failed"
    assert failed["recovery"]["option"] == "center_crop" and "purple unicorn" in failed["recovery"]["message"]
    assert failed["plan"]["edit"]["cutout"]["subject"] == "purple unicorn"
    manager.render(ident, {"plan": chosen, "tracking": False})
    manager.process(ident)
    done = manager.get(ident)
    assert done["status"] == "complete", done["error"]
    assert http.paid() == 1
    assert any("no cutout" in warning for warning in done["result"]["warnings"])
    assert '"cutout"' not in json.dumps([op["op"] for op in []]) and "op: cutout" not in manager.artifact(ident, "brief").read_text(encoding="utf-8")


@pytest.mark.render
def test_local_mode_never_calls_meta_and_says_the_background_is_unchanged(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Local mode called Meta")

    monkeypatch.setattr(meta, "_request", forbidden)
    job = manager.create({"paths": [str(footage(tmp_path))], "goal": "Show the product clearly", "target_duration": 1, "captions": False, "aspect": "16:9"})
    manager.process(job["id"])
    plan = manager.get(job["id"])["plan"]
    manager.render(job["id"], {"plan": with_cutout(plan, mode="blur", subject="the mug")})
    manager.process(job["id"])
    done = manager.get(job["id"])
    assert done["status"] == "complete", done["error"]
    assert any("background is unchanged" in warning for warning in done["result"]["warnings"])


# --- Generated backgrounds ---

def test_a_background_follows_the_footage_shape_and_is_bounded() -> None:
    assert meta.background_size(1280, 720) == "1792x1008"
    assert meta.background_size(1080, 1920) == "1008x1792"
    assert meta.background_size(1000, 1000) == "1792x1792"
    assert meta.background_size(0, 0) == "1792x1024"
    assert meta.background_size(16000, 900) == "1792x256"


def test_generate_background_sends_words_only_and_is_billed_once(enabled: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    work = tmp_path / "work"
    first = meta.generate_background("a marble counter", size="1792x1008", work_dir=work)
    second = meta.generate_background("a marble counter", size="1792x1008", work_dir=work)
    assert http.paid("/images/generations") == 1 and first["path"] == second["path"] and second["usage"]["cached"] is True
    assert Path(first["path"]).read_bytes() == PNG
    sent = next(kwargs for method, path, kwargs in http.calls if path == "/images/generations")["payload"]
    assert sent["model"] == "muse-image-1.0" and sent["size"] == "1792x1008" and sent["n"] == 1 and sent["output_format"] == "png"
    assert not http.paid("/files")
    assert ledger(work)[0]["usage"]["cost_usd"] == pytest.approx(0.01)


def test_a_project_keeps_a_generated_background_as_an_image_asset(enabled: None, manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ident, plan = planned_project(manager, tmp_path, monkeypatch, provider="meta")
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    asset = manager.generate_background(ident, {"prompt": "  a marble kitchen counter, soft window light  ", "consent": True})
    assert asset["kind"] == "image" and (asset["width"], asset["height"]) == (16, 9) and asset["name"].endswith(".png")
    assert "path" not in asset
    job = manager.get(ident)
    assert asset["id"] in job["assets"] and "path" not in job["assets"][asset["id"]]
    stored = manager.asset_path(_raw_job(manager, ident), asset["id"])
    assert stored.read_bytes() == PNG
    sent = next(kwargs for method, path, kwargs in http.calls if path == "/images/generations")["payload"]
    assert sent["prompt"] == "a marble kitchen counter, soft window light" and sent["size"] == "1792x1008"
    # The edit can now choose it as a cutout background.
    from vedit.campaign import validate_plan

    chosen = validate_plan(with_cutout(plan, mode="image", subject="the mug", asset=asset["id"]), 1.6, original=plan, assets=_raw_job(manager, ident)["assets"])
    assert chosen["edit"]["cutout"]["asset"] == asset["id"]


def _raw_job(manager: CampaignManager, ident: str) -> dict[str, Any]:
    with manager._lock, manager._connect() as connection:
        return manager._load(connection, ident)


@pytest.mark.parametrize("value, status, words", [("text", 400, "JSON object"), ({"prompt": ""}, 400, "prompt"), ({"prompt": "x", "consent": False}, 409, "Confirm"), ({"prompt": "x"}, 409, "Confirm"), ({"prompt": "x", "consent": "yes"}, 409, "Confirm")])
def test_a_background_needs_a_prompt_and_consent(enabled: None, manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: Any, status: int, words: str) -> None:
    ident, _ = planned_project(manager, tmp_path, monkeypatch, provider="meta")
    http = Http()
    monkeypatch.setattr(meta, "_request", http)
    with pytest.raises(CampaignError, match=words) as caught:
        manager.generate_background(ident, value)
    assert caught.value.status == status and http.calls == []


def test_a_local_project_cannot_generate_a_background(manager: CampaignManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Local mode called Meta")

    monkeypatch.setattr(meta, "_request", forbidden)
    job = manager.create({"paths": [str(footage(tmp_path))], "goal": "Show the product clearly", "target_duration": 1, "captions": False})
    manager.process(job["id"])
    with pytest.raises(CampaignError, match="Meta mode") as caught:
        manager.generate_background(job["id"], {"prompt": "a marble counter", "consent": True})
    assert caught.value.status == 409


@pytest.mark.render
def test_an_upright_clip_is_tracked_upright_but_still_planned_wide(manager: CampaignManager, tmp_path: Path) -> None:
    """Masks are only as sharp as what SAM saw: 540x960 of picture beats 304x540 at the same price."""
    upright = tmp_path / "upright.mp4"
    ffmpeg("-f", "lavfi", "-i", "color=white:s=1080x1920:r=30:d=1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(upright))
    info = probe(upright)
    assembly = {"op": "select_ranges", "ranges": [{"start": 0.1, "end": 0.7}]}
    tracked = probe(manager._analysis_proxy(tmp_path / "project", upright, info, assembly))
    assert (tracked.width, tracked.height) == (540, 960)
    assert matte.picture_area(info.square_width, info.display_height, tracked.width, tracked.height) == matte.Area(0, 0, 540, 960)
    assert manager._analysis_proxy(tmp_path / "project", upright, info, assembly).stat().st_size  # and its cache still answers
    planned = probe(manager._analysis_proxy(tmp_path / "project", upright, info))
    assert (planned.width, planned.height) == (960, 540)


def test_capabilities_offer_cutouts_only_with_the_decoder(enabled: None) -> None:
    status = capabilities()["meta"]
    assert status["features"]["cutout"] is True and status["features"]["background"] is True
    assert status["models"]["tracking"] == "sam-3.1"
    assert capabilities()["editor"]["features"]["cutout"] is True


@pytest.fixture
def studio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("VEDIT_WORKSPACE", str(tmp_path))
    httpd = _LoopbackServer(("127.0.0.1", 0), StudioHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield int(httpd.server_address[1])
    finally:
        httpd.shutdown()
        httpd.server_close()
        from vedit.campaign import get_manager

        get_manager.cache_clear() if hasattr(get_manager, "cache_clear") else None


def call(port: int, method: str, path: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    headers = {"Host": f"127.0.0.1:{port}"}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode()
        headers.update({"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
    connection.request(method, path, payload, headers)
    response = connection.getresponse()
    data = json.loads(response.read() or b"{}")
    connection.close()
    return response.status, data


def test_the_studio_route_exists_for_the_ui_probe_and_reaches_the_project(studio: int) -> None:
    # mediabin.js decides whether to offer this button by probing the route with GET.
    status, data = call(studio, "GET", "/api/campaigns/probe/background")
    assert status == 405 and "route not found" not in data["error"].lower()
    status, data = call(studio, "POST", "/api/campaigns/no-such-project/background", {"prompt": "a marble counter", "consent": True})
    assert status == 404 and "route not found" not in data["error"].lower()


def test_a_dead_decoder_worker_costs_speed_and_never_the_render(monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import BrokenExecutor, Future

    from vedit import matte

    class DeadPool:
        def __init__(self, *args, **kwargs) -> None:
            self.calls = 0

        def submit(self, *args):
            self.calls += 1
            if self.calls > 2:
                raise BrokenExecutor("pool is gone")
            future: Future = Future()
            future.set_exception(BrokenExecutor("worker died"))
            return future

        def shutdown(self, **kwargs) -> None:
            pass

    monkeypatch.setattr(matte, "ProcessPoolExecutor", DeadPool)
    monkeypatch.setattr(matte, "read_mask", lambda raw, span: (bytes([span[0]]), 1, 1))
    spans = [(index, index + 1) for index in range(matte.INLINE_FRAMES + 4)]
    decoded = list(matte._decoded("raw", spans, workers=4))
    assert [item[0][0] for item in decoded] == [span[0] for span in spans]
