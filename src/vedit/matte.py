"""A grayscale matte of the chosen subject, one frame for every finished frame.

SAM reads the analysis proxy, and the proxy is assembled by the same
``select_ranges`` as the render, so frame N of SAM's answer is frame N of the
finished video. Nothing here retimes anything.

Each mask covers only its object's box, in proxy pixels, so every frame is
pasted onto a blank picture. The proxy may also letterbox a clip whose shape is
not 16:9, so the matte is sized to the picture inside the proxy and the compiler
can stretch it straight onto the source. Frames stream through FFmpeg's stdin:
this process never holds more than a short window of decoded masks.
"""

from __future__ import annotations

import multiprocessing
import os
import subprocess
import tempfile
import threading
from collections import deque
from collections.abc import Generator, Iterable
from concurrent.futures import BrokenExecutor, Future, ProcessPoolExecutor
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import IO, Any

from vedit.compile import _frame_windows
from vedit.errors import RenderCancelled, RenderError
from vedit.media import probe, resolve_ffmpeg
from vedit.meta_sam import read_mask

MAX_GAP_SECONDS = 0.5
INLINE_FRAMES = 8
_OPAQUE = bytes(range(128, 256))
# Adaptive, so an edge that shimmers is averaged but a moving subject or a hard
# cut is left alone; tmix would blend the two shots either side of a cut. The
# public decoder returns hard masks, so the feather supplies the soft edge.
MATTE_FILTER = "atadenoise=0a=0.2:0b=0.4:s=5:p=1,gblur=sigma=1.5"


class MatteError(RenderError):
    """The matte could not be built from these masks."""


@dataclass(frozen=True)
class Area:
    """The picture inside the proxy, in proxy pixels."""

    left: int
    top: int
    width: int
    height: int


def picture_area(source_width: int, source_height: int, proxy_width: int, proxy_height: int) -> Area:
    """Where ``vf_fit`` put the source: FFmpeg's scale rounding and pad's even offsets."""
    if min(source_width, source_height, proxy_width, proxy_height) <= 0:
        raise MatteError("The clip has no usable size for a cutout.")
    width = min(proxy_width, (2 * proxy_height * source_width + source_height) // (2 * source_height))
    height = min(proxy_height, (2 * proxy_width * source_height + source_width) // (2 * source_width))
    return Area(((proxy_width - width) // 2) & ~1, ((proxy_height - height) // 2) & ~1, width, height)


def finished_frames(view: dict[str, Any], fps: float) -> tuple[int, list[int]]:
    """The finished video's frame count and where each later moment starts, as the compiler rounds them."""
    windows = _frame_windows(view["spans"], len(view["segments"]), fps, overlapping=view["transition_seconds"] > 0)
    return windows[-1][1], [first for first, _ in windows[1:]]


def nearest_frames(
    available: Iterable[int], frames: int, *, cuts: Iterable[int] = (), reach: int = 0
) -> list[int | None]:
    """For every output frame, the mask frame to show, or None when the subject is gone.

    A frame SAM skipped borrows the closest mask up to ``reach`` frames away, but
    never across a cut: that mask belongs to a different picture.
    """
    have = sorted(set(available))
    edges = sorted({0, frames, *(cut for cut in cuts if 0 < cut < frames)})
    chosen: list[int | None] = [None] * frames
    cursor = 0
    for shot, stop in pairwise(edges):
        while cursor < len(have) and have[cursor] < shot:
            cursor += 1
        inside = cursor
        for frame in range(shot, stop):
            while inside + 1 < len(have) and have[inside + 1] <= frame:
                inside += 1
            best = None
            for candidate in have[inside : inside + 2]:
                if (
                    shot <= candidate < stop
                    and abs(candidate - frame) <= reach
                    and (best is None or abs(candidate - frame) < abs(best - frame))
                ):
                    best = candidate
            chosen[frame] = best
    return chosen


def paste(raster: bytes, columns: int, rows: int, box: tuple[int, int, int, int], area: Area) -> bytearray:
    """Place a box-local mask on a blank picture, clipped to the picture."""
    canvas = bytearray(area.width * area.height)
    left, top, right, bottom = box
    x0, x1 = max(left, area.left), min(right, area.left + area.width)
    y0, y1 = max(top, area.top), min(bottom, area.top + area.height)
    if x0 >= x1 or y0 >= y1:
        return canvas
    across, down = right - left, bottom - top
    # SAM 3.1 sends one mask pixel per box pixel; the protocol only promises "smaller than the frame".
    pick = None if columns == across else [(x - left) * columns // across for x in range(x0, x1)]
    for y in range(y0, y1):
        start = (y - top) * rows // down * columns
        piece = (
            raster[start + x0 - left : start + x1 - left] if pick is None else bytes(raster[start + at] for at in pick)
        )
        offset = (y - area.top) * area.width + x0 - area.left
        canvas[offset : offset + x1 - x0] = piece
    return canvas


def _workers() -> int:
    return max(1, min(8, (os.cpu_count() or 2) - 1))


def _decoded(raw: str, spans: list[tuple[int, int]], workers: int) -> Generator[tuple[bytes, int, int]]:
    """Masks in order. Decoding is pure Python, so it runs in processes, a bounded window at a time."""
    if workers <= 1 or len(spans) < INLINE_FRAMES:
        for span in spans:
            yield read_mask(raw, span)
        return
    pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context("spawn"))
    pending: deque[tuple[Future[tuple[bytes, int, int]], tuple[int, int]]] = deque()
    broken = False

    def collect() -> tuple[bytes, int, int]:
        # A worker can die under memory pressure. That loses speed, never the render.
        nonlocal broken
        future, span = pending.popleft()
        if not broken:
            try:
                return future.result()
            except BrokenExecutor:
                broken = True
        return read_mask(raw, span)

    try:
        for span in spans:
            if broken:
                yield read_mask(raw, span)
                continue
            try:
                pending.append((pool.submit(read_mask, raw, span), span))
            except BrokenExecutor:
                broken = True
                while pending:
                    yield collect()
                yield read_mask(raw, span)
                continue
            if len(pending) >= workers * 3:
                yield collect()
        while pending:
            yield collect()
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def flicker(areas: list[int], cuts: Iterable[int] = ()) -> float:
    """The 95th percentile of frame-to-frame area change inside a shot, as a share of the area."""
    edges = set(cuts)
    changes = sorted(
        abs(now - before) / max(now, before)
        for index, (before, now) in enumerate(pairwise(areas), 1)
        if index not in edges and before and now
    )
    return changes[int(len(changes) * 0.95)] if changes else 0.0


def report(
    areas: list[int], *, masked: int, filled: int, picture: int, fps: float, cuts: Iterable[int] = ()
) -> dict[str, Any]:
    """What the receipts need, measured from the masks that really went into the matte."""
    frames = len(areas)
    missing = next((index for index, area in enumerate(areas) if not area), None)
    return {
        "frames": frames,
        "masked_frames": masked,
        "filled_frames": filled,
        "empty_frames": frames - masked - filled,
        "coverage": round(masked / frames, 4) if frames else 0.0,
        "flicker": round(flicker(areas, cuts), 4),
        "mean_area": round(sum(areas) / frames / picture, 4) if frames and picture else 0.0,
        "first_missing": round(missing / fps, 3) if missing is not None else None,
    }


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise RenderCancelled("The render was cancelled.")


def _stdin_of(process: subprocess.Popen[bytes]) -> IO[bytes]:
    """The pipe a process was opened with. ``stdin=PIPE`` always gives one; the type just cannot say so."""
    if process.stdin is None:
        raise MatteError("FFmpeg was started without a pipe for the matte frames.")
    return process.stdin


def build_matte(
    masks: dict[str, Any],
    *,
    path: Path,
    area: Area,
    frames: int,
    cuts: Iterable[int] = (),
    cancel_event: threading.Event | None = None,
    filters: str = MATTE_FILTER,
) -> dict[str, Any]:
    """Write ``path`` and return its ``report``. ``masks`` is ``meta.subject_masks``'s result."""
    entries = {entry["frame"]: entry for entry in masks.get("frames") or []}
    if frames <= 0 or not entries:
        raise MatteError("There are no subject masks to build a cutout from.")
    fps = float(masks["fps"])
    cuts = sorted(cuts)
    chosen = nearest_frames(entries, frames, cuts=cuts, reach=round(MAX_GAP_SECONDS * fps))
    # A held mask repeats in the output, not in the decode queue.
    needed = sorted({frame for frame in chosen if frame is not None})
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.stem + ".part" + path.suffix)
    argv = [
        resolve_ffmpeg(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "gray",
        "-video_size",
        f"{area.width}x{area.height}",
        "-framerate",
        f"{fps:.6f}",
        "-i",
        "pipe:0",
        "-vf",
        filters,
        "-c:v",
        "ffv1",
        "-level",
        "3",
        "-pix_fmt",
        "gray",
        str(pending),
    ]
    blank = bytes(area.width * area.height)
    areas: list[int] = []
    with tempfile.TemporaryFile() as errors:
        ffmpeg = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=errors)
        feed = _stdin_of(ffmpeg)
        decoded = _decoded(masks["raw"], [tuple(entries[frame]["span"]) for frame in needed], _workers())
        try:
            held, held_area, shown = blank, 0, None
            for want in chosen:
                _check_cancelled(cancel_event)
                if want is None:
                    picture, value = blank, 0
                else:
                    if want != shown:
                        raster, columns, rows = next(decoded)
                        held = paste(raster, columns, rows, tuple(entries[want]["box"]), area)
                        held_area = len(held) - len(held.translate(None, _OPAQUE))
                        shown = want
                    picture, value = held, held_area
                feed.write(picture)
                areas.append(value)
            feed.close()
            code = ffmpeg.wait()
        except BrokenPipeError:
            code = ffmpeg.wait()
        except BaseException:
            ffmpeg.kill()
            ffmpeg.wait()
            pending.unlink(missing_ok=True)
            raise
        finally:
            decoded.close()
        if code != 0:
            errors.seek(0)
            pending.unlink(missing_ok=True)
            raise MatteError(
                "FFmpeg could not write the cutout matte: " + errors.read()[-400:].decode("utf-8", "replace").strip()
            )
    pending.replace(path)
    made = probe(path)
    if (made.width, made.height) != (area.width, area.height) or abs(made.duration - frames / fps) > 2 / fps:
        raise MatteError("The cutout matte did not match the finished video.")
    real = sum(1 for index, want in enumerate(chosen) if want == index)
    filled = sum(1 for index, want in enumerate(chosen) if want is not None and want != index)
    return report(areas, masked=real, filled=filled, picture=area.width * area.height, fps=fps, cuts=cuts)
