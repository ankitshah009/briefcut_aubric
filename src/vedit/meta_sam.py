"""SAM 3.1's answer, read: frame records, boxes, one followed identity, and mask decoding.

Nothing here talks to Meta. ``vedit.meta`` makes and stores the call; this
module turns the stored text into tracking points and mask indexes, and
decodes one mask at a time with Meta's own public decoder. The mask wire
format belongs to ``meta-sam-parser`` (the ``sam`` extra, under Meta's SAM
License), which bars reverse engineering, so the codec is never reimplemented
and only that package's public functions are called.
"""

from __future__ import annotations

import heapq
import math
import re
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

from vedit.meta_base import MetaError, _number

_FRAME = re.compile(r"^<(\d+)f>", re.MULTILINE)
_BOX = re.compile(
    r"(\d+)<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)"
    r"(?:;c=([0-9.eE+-]+))?\|>"
)
_MASK_OPEN = "<|mask;"
_MASK_CLOSE = "|>"
_MAX_TRACK_POINTS = 512
_LARGEST_GAP_SECONDS = 0.5  # a subject hidden longer than this is not interpolated
_LEAST_SEEN_SHARE = 0.9  # the share of frames the followed subject must appear in
_RASTER = bytes((0, 255)) + bytes(254)


def _sam_records(text: str, *, fps: float, duration: float) -> Iterator[dict[str, Any]]:
    """Walk SAM's lines as ``{frame, object, point, box, size, mask}`` records.

    ``point`` is the normalized box tracking uses. ``box`` is half-open proxy
    pixels: the wire's ``x2`` and ``y2`` are inclusive. ``mask`` is the span of
    the mask token inside ``text``; nothing here decodes it.
    """
    if not text.strip():
        return
    frame_markers = list(_FRAME.finditer(text))
    if not frame_markers or text[: frame_markers[0].start()].strip():
        raise MetaError("SAM returned unreadable tracking data. No tracking was applied.")
    for index, marker in enumerate(frame_markers):
        frame = int(marker.group(1))
        when = frame / fps
        if when > duration + 1 / fps:
            raise MetaError("SAM returned a frame outside the source duration.")
        line = text[marker.end() : frame_markers[index + 1].start() if index + 1 < len(frame_markers) else len(text)]
        chunk = line.strip()
        begin = marker.end() + len(line) - len(line.lstrip())
        for box, mask_start, mask_end in _frame_records(chunk):
            yield _record(box, frame=frame, when=min(when, duration), mask=(begin + mask_start, begin + mask_end))


def _frame_records(chunk: str) -> Iterator[tuple[re.Match[str], int, int]]:
    """Each ``box`` match in one frame's line with the span of the mask token that follows it.

    Mask bytes are opaque base85: the scan steps past each token and never
    searches its contents for apparent frame markers or box records.
    """
    cursor = 0
    while cursor < len(chunk):
        box = _BOX.match(chunk, cursor)
        if box is None or not chunk.startswith(_MASK_OPEN, box.end()):
            raise MetaError("SAM returned malformed tracking records. No tracking was applied.")
        mask_end = chunk.find(_MASK_CLOSE, box.end() + len(_MASK_OPEN))
        if mask_end < 0:
            raise MetaError("SAM returned an incomplete mask record. No tracking was applied.")
        cursor = mask_end + len(_MASK_CLOSE)
        if cursor < len(chunk):
            if chunk[cursor] != ",":
                raise MetaError("SAM returned malformed tracking records. No tracking was applied.")
            cursor += 1
        yield box, box.end(), mask_end + len(_MASK_CLOSE)


def _record(box: re.Match[str], *, frame: int, when: float, mask: tuple[int, int]) -> dict[str, Any]:
    """One box match as a record, with its coordinates checked against the frame it sits in."""
    ident, x1, y1, x2, y2, width, height = (int(value) for value in box.groups()[:7])
    # x2 and y2 are inclusive, so a one-pixel detection has x1 == x2 and the last column is width - 1.
    if not (width > 0 and height > 0 and 0 <= x1 <= x2 < width and 0 <= y1 <= y2 < height):
        raise MetaError("SAM returned invalid subject coordinates. No tracking was applied.")
    point: dict[str, Any] = {
        "time": round(when, 6),
        "x": x1 / width,
        "y": y1 / height,
        "width": (x2 - x1 + 1) / width,
        "height": (y2 - y1 + 1) / height,
    }
    confidence = box.group(8)
    if confidence is not None:
        try:
            point["confidence"] = _number(float(confidence), "SAM confidence", maximum=1)
        except ValueError:
            raise MetaError("SAM returned invalid confidence data.") from None
    return {
        "frame": frame,
        "object": ident,
        "point": point,
        "box": (x1, y1, x2 + 1, y2 + 1),
        "size": (width, height),
        "mask": mask,
    }


def _selected_object(tracks: dict[int, dict[int, dict[str, Any]]]) -> int:
    """Follow one identity consistently; never jump between people frame-by-frame."""
    if not tracks:
        raise MetaError("SAM returned frame records without usable boxes. No tracking was applied.")
    return max(
        tracks,
        key=lambda ident: (
            len(tracks[ident]),
            sum(item["point"]["width"] * item["point"]["height"] for item in tracks[ident].values()),
            -ident,
        ),
    )


def _sam_tracks(text: str, *, fps: float, duration: float) -> dict[int, dict[int, dict[str, Any]]]:
    """Group records by object, then frame. A frame SAM repeats keeps its latest record."""
    tracks: dict[int, dict[int, dict[str, Any]]] = {}
    for record in _sam_records(text, fps=fps, duration=duration):
        tracks.setdefault(record["object"], {})[record["frame"]] = record
    return tracks


def _tracking_points(text: str, *, fps: float, duration: float) -> tuple[list[dict[str, Any]], int | None]:
    """Read box records; deliberately ignore the mask payload rather than decoding it."""
    if not text.strip():
        return [], None
    tracks = _sam_tracks(text, fps=fps, duration=duration)
    selected = _selected_object(tracks)
    points = [record["point"] for _, record in sorted(tracks[selected].items())]
    expected = max(1, math.ceil(duration * fps))
    gaps = [points[0]["time"], max(0, duration - points[-1]["time"] - 1 / fps)]
    gaps.extend(later["time"] - earlier["time"] - 1 / fps for earlier, later in pairwise(points))
    if max(gaps) > _LARGEST_GAP_SECONDS or len(points) / expected < _LEAST_SEEN_SHARE:
        raise MetaError(
            "The selected subject is hidden or missing for part of this clip. Select a shorter visible range or use a manual crop; uncertain motion was not interpolated."
        )
    return _simplify_track(points), selected


def _simplify_track(points: list[dict[str, Any]], maximum: int = _MAX_TRACK_POINTS) -> list[dict[str, Any]]:
    """Keep motion changes using time-aware interpolation error, bounded for codegen."""
    if len(points) <= maximum:
        return points
    chosen = {0, len(points) - 1}
    heap: list[tuple[float, int, int, int]] = []

    def queue(left: int, right: int) -> None:
        if right - left <= 1:
            return
        first, last = points[left], points[right]
        span = last["time"] - first["time"]
        worst, at = 0.0, left + 1
        for index in range(left + 1, right):
            point = points[index]
            mix = (point["time"] - first["time"]) / span if span > 0 else 0
            error = max(
                abs(point[key] - (first[key] + (last[key] - first[key]) * mix)) for key in ("x", "y", "width", "height")
            )
            if error > worst:
                worst, at = error, index
        heapq.heappush(heap, (-worst, at, left, right))

    queue(0, len(points) - 1)
    while heap and len(chosen) < maximum:
        negative_error, at, left, right = heapq.heappop(heap)
        if -negative_error <= 0.001:
            break
        chosen.add(at)
        queue(left, at)
        queue(at, right)
    return [points[index] for index in sorted(chosen)]


def _validate_cached_track(value: dict[str, Any], duration: float) -> None:
    """Refuse a cached track whose points are not a strictly timed sequence of boxes inside the frame."""
    points = value.get("points")
    if not isinstance(points, list) or len(points) > _MAX_TRACK_POINTS:
        raise MetaError("The cached subject track is invalid. Start a new campaign.")
    previous = -1.0
    for point in points:
        if not isinstance(point, dict):
            raise MetaError("The cached subject track is invalid. Start a new campaign.")
        when = _number(point.get("time"), "Cached tracking time", maximum=duration)
        if when <= previous:
            raise MetaError("The cached tracking times are invalid. Start a new campaign.")
        previous = when
        x = _number(point.get("x"), "Cached tracking x", maximum=1)
        y = _number(point.get("y"), "Cached tracking y", maximum=1)
        width = _number(point.get("width"), "Cached tracking width", minimum=0.000001, maximum=1)
        height = _number(point.get("height"), "Cached tracking height", minimum=0.000001, maximum=1)
        if x + width > 1.000001 or y + height > 1.000001:
            raise MetaError("The cached subject box leaves the frame. Start a new campaign.")


def _mask_fields(token: str) -> tuple[int, int, str]:
    """``(height, width, payload)`` of one ``<|mask;...|>`` token, or a ``MetaError``."""
    fields: dict[str, str] = {}
    for field in token.removeprefix("<|").removesuffix("|>").split(";"):
        name, _, value = field.partition("=")
        fields[name] = value
    try:
        rows, columns, payload = fields["data"].split(",", 2)
        height, width = int(rows), int(columns)
    except (KeyError, ValueError):
        raise MetaError("SAM returned an unreadable mask. No cutout was applied.") from None
    if width <= 0 or height <= 0 or payload[:1] not in ("~", "!"):
        raise MetaError("SAM returned an unreadable mask. No cutout was applied.")
    return height, width, payload


def decode_mask(token: str) -> tuple[bytes, int, int]:
    """Decode one ``<|mask;...|>`` token to ``(coverage, columns, rows)``.

    Coverage is row-major bytes, 0 or 255, covering the record's box and not
    the frame. Only ``meta-sam-parser``'s public decoder is used; the matte's
    feather supplies the soft edge.
    """
    try:
        from meta_sam_parser import SegmentationMask, decode_mask_to_raster
    except ImportError:
        raise MetaError("Cutouts need Meta's mask decoder. Run: uv sync --extra sam") from None
    height, width, payload = _mask_fields(token)
    encoding = "lossless" if payload[0] == "~" else "one_bit"
    try:
        raster = decode_mask_to_raster(SegmentationMask(encoding=encoding, payload=payload, width=width, height=height))
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 - the third-party codec's failures are its own; every one becomes this MetaError
        raise MetaError("SAM returned an unreadable mask. No cutout was applied.") from None
    return raster.translate(_RASTER), width, height


def read_mask(raw: str | Path, span: tuple[int, int]) -> tuple[bytes, int, int]:
    """Decode one mask from a stored SAM response by its ``span``."""
    start, end = span
    with Path(raw).open("rb") as handle:
        handle.seek(start)
        return decode_mask(handle.read(end - start).decode("latin-1"))
