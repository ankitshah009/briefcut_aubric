"""Camera moves on the output clock, and the scaling a half-size stabilizer pass needs.

A zoom is a view rectangle moving over the frame. ``perspective`` renders that
rectangle at sub-pixel precision. Measured on a slow 8% push at 1080x1920,
tracking a marker through every frame, with jitter as the RMS of its
frame-to-frame acceleration:

=======================  ==========  ========
method                   jitter      speed
=======================  ==========  ========
zoompan                  0.9-1.3 px  500 fps
zoompan, 4x upscale      0.3-0.5 px  65 fps
perspective, this file   0.04 px     114 fps
=======================  ==========  ========

Everything here is on whole frames, rounded the way ``select_ranges`` rounds its
cuts, so a move changes on exactly the frame the picture cuts.
"""

from __future__ import annotations

import argparse
import math
import re
import statistics
import subprocess
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from typing import Any

MIN_SCALE, MAX_SCALE = 1.0, 3.0
SLOW_ZOOM = 0.18
PUNCH_ZOOM = 0.3
PUNCH_SECONDS = 0.18
PUNCH_START = 0.55  # a punch lands this far in on the cut, and settles the rest over PUNCH_SECONDS
IDENTITY = (1.0, 0.5, 0.5)
STABILIZE_SHAKE_WARNING = 0.75

EASES = ("linear", "gentle", "smooth", "out")
_EASE_EXPRESSION = {
    "linear": "ld(0)",
    "gentle": "ld(0)*(0.5+0.5*ld(0)*(3-2*ld(0)))",
    "smooth": "ld(0)*ld(0)*(3-2*ld(0))",
    "out": "1-pow(1-ld(0),3)",
}


class MotionError(ValueError):
    """A move cannot be rendered."""


def _block_shift(first: bytes, second: bytes, x0: int, y0: int, width: int, height: int) -> tuple[int, int, int]:
    """Find a textured block's whole-pixel move between two small gray frames."""
    area = width * height
    texture = sum(
        (first[y * 160 + x] - first[y0 * 160 + x0]) ** 2
        for y in range(y0, y0 + height, 2)
        for x in range(x0, x0 + width, 2)
    )
    if texture < 1200:
        return 0, 0, 0
    best, shift = float("inf"), (0, 0)
    for dy in range(-4, 5):
        for dx in range(-4, 5):
            total = 0
            for y in range(y0 + 2, y0 + height - 2, 2):
                other_y = y + dy
                if not 0 <= other_y < 90:
                    continue
                row, other = y * 160, other_y * 160
                for x in range(x0 + 2, x0 + width - 2, 2):
                    other_x = x + dx
                    if not 0 <= other_x < 160:
                        continue
                    total += abs(first[row + x] - second[other + other_x])
            if total < best:
                best, shift = total, (dx, dy)
    return shift[0], shift[1], area


def measure_camera_shake(path: str | Path, *, seconds: float = 12.0, ffmpeg: str | None = None) -> float | None:
    """Estimate residual whole-frame camera motion in a short low-resolution sample.

    The returned RMS is in pixels at 160x90 after removing the median pan speed.
    A low value is a reason to warn before applying stabilization, not a quality
    score. ``None`` means there was too little textured motion to measure safely.
    """
    from vedit.errors import VeditError
    from vedit.media import probe, resolve_ffmpeg

    binary = ffmpeg or resolve_ffmpeg()
    try:
        # FFmpeg's own pick is the largest picture, which can be a cover image or a still proxy.
        footage = probe(path).video_map
    except VeditError:
        return None
    try:
        result = subprocess.run(
            [
                binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(path),
                "-map",
                f"0:v:{footage}",
                "-t",
                f"{seconds:.3f}",
                "-an",
                "-vf",
                "fps=4,scale=160:90:force_original_aspect_ratio=decrease,pad=160:90:(ow-iw)/2:(oh-ih)/2,format=gray",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "-",
            ],
            capture_output=True,
            timeout=max(120.0, seconds * 12),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    frame_bytes = 160 * 90
    raw = result.stdout
    if result.returncode or len(raw) < frame_bytes * 4:
        return None
    frames = [raw[offset : offset + frame_bytes] for offset in range(0, len(raw) - frame_bytes + 1, frame_bytes)]
    steps: list[tuple[float, float]] = []
    for first, second in pairwise(frames):
        shifts = []
        for y in range(8, 80, 14):
            for x in range(8, 144, 18):
                dx, dy, weight = _block_shift(first, second, x, y, 16, 12)
                if weight:
                    shifts.append((dx, dy))
        if len(shifts) < 4:
            continue
        steps.append((statistics.median(dx for dx, _ in shifts), statistics.median(dy for _, dy in shifts)))
    if len(steps) < 3:
        return None
    median_x = statistics.median(dx for dx, _ in steps)
    median_y = statistics.median(dy for _, dy in steps)
    return math.sqrt(sum((dx - median_x) ** 2 + (dy - median_y) ** 2 for dx, dy in steps) / len(steps))


def ease(kind: str, p: float) -> float:
    """The same curves as ``_EASE_EXPRESSION``, for tests and previews."""
    if kind == "gentle":
        return p * (0.5 + 0.5 * p * (3 - 2 * p))
    if kind == "smooth":
        return p * p * (3 - 2 * p)
    if kind == "out":
        return 1 - (1 - p) ** 3
    return p


def _state(raw: Any, label: str) -> tuple[float, float, float]:
    """``(scale, x, y)`` with the centre pulled in so the view never leaves the frame."""
    if not isinstance(raw, dict):
        raise MotionError(f"{label} must be a keyframe with scale, x, and y.")
    values = []
    for key, fallback in (("scale", 1.0), ("x", 0.5), ("y", 0.5)):
        value = raw.get(key, fallback)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise MotionError(f"{label} {key} must be a number.")
        values.append(float(value))
    scale = min(MAX_SCALE, max(MIN_SCALE, values[0]))
    half = 0.5 / scale
    return scale, min(1 - half, max(half, values[1])), min(1 - half, max(half, values[2]))


@dataclass(frozen=True)
class Piece:
    """Frames ``[first, last)`` moving from one state to another."""

    first: int
    last: int
    begin: tuple[float, float, float]
    end: tuple[float, float, float]
    curve: str = "gentle"

    def state(self, frame: int) -> tuple[float, float, float]:
        if self.begin == self.end:
            return self.begin
        p = max(0.0, min(1.0, (frame - self.first) / max(1, self.last - 1 - self.first)))
        e = ease(self.curve, p)
        ratio = self.end[0] / self.begin[0]
        return (
            self.begin[0] * ratio**e,
            self.begin[1] + (self.end[1] - self.begin[1]) * e,
            self.begin[2] + (self.end[2] - self.begin[2]) * e,
        )


def plan_pieces(
    moves: list[dict[str, Any]], fps: float, *, blend: float = 0.0, end: float | None = None
) -> list[Piece]:
    """Whole-frame pieces for validated moves, in order and never overlapping.

    A transition blends two pictures that both pass through this one zoom, so
    across an overlap the state walks from one move to the next and the picture
    never jumps mid-blend. ``blend`` is the transition's length: a moment that
    borders a still one eases in and out of the full frame over it. ``end`` is
    where the tape stops, since the last moment has nothing to blend into.
    """
    if fps <= 0:
        raise MotionError("Zoom needs a positive frame rate.")
    bridge = round(blend * fps)
    tape_end = round(end * fps) if end is not None else None
    timed = []
    for index, move in enumerate(moves):
        if not isinstance(move, dict):
            raise MotionError(f"Zoom move {index + 1} must be an object.")
        start, stop = move.get("start"), move.get("end")
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or isinstance(stop, bool)
            or not isinstance(stop, (int, float))
            or not math.isfinite(start)
            or not math.isfinite(stop)
            or start < 0
            or stop <= start
        ):
            raise MotionError(f"Zoom move {index + 1} needs a start and a later end.")
        curve = move.get("ease", "gentle")
        if curve not in EASES:
            raise MotionError(f"Zoom move {index + 1} has an unknown easing.")
        settle = move.get("settle")
        if settle is not None and (
            isinstance(settle, bool) or not isinstance(settle, (int, float)) or not math.isfinite(settle) or settle <= 0
        ):
            raise MotionError(f"Zoom move {index + 1} has an invalid settle time.")
        first, last = round(start * fps), max(round(stop * fps), round(start * fps) + 1)
        timed.append(
            (
                first,
                last,
                _state(move.get("from"), f"Zoom move {index + 1}"),
                _state(move.get("to"), f"Zoom move {index + 1}"),
                curve,
                settle,
            )
        )
    timed.sort(key=lambda item: item[0])
    pieces: list[Piece] = []
    for index, (first, last, begin, finish, curve, settle) in enumerate(timed):
        before = timed[index - 1] if index else None
        after = timed[index + 1] if index + 1 < len(timed) else None
        head = max(0, before[1] - first) if before else 0
        tail = max(0, last - after[0]) if after else 0
        # A still moment on either side: nothing overlaps this move, but the transition still does.
        ease_in = min(bridge, last - first) if bridge and not head and first > 0 else 0
        ease_out = (
            min(bridge, last - first - ease_in) if bridge and not tail and (tape_end is None or last < tape_end) else 0
        )
        body_start, body_end = first + max(head, ease_in), max(first + max(head, ease_in), last - max(tail, ease_out))
        if ease_in:
            pieces.append(Piece(first, body_start, IDENTITY, begin, "smooth"))
        if body_end > body_start:
            if settle is not None and not (head or ease_in):
                split = min(body_end, body_start + max(2, round(settle * fps)))
                landing = (1 + (begin[0] - 1) * PUNCH_START, begin[1], begin[2])
                pieces.append(
                    Piece(
                        body_start,
                        split,
                        _state({"scale": landing[0], "x": landing[1], "y": landing[2]}, "Zoom"),
                        begin,
                        "out",
                    )
                )
                if split < body_end:
                    pieces.append(Piece(split, body_end, begin, finish, curve))
            else:
                pieces.append(Piece(body_start, body_end, begin, finish, curve))
        if after is not None and tail:
            pieces.append(Piece(body_end, last, finish, after[2], "smooth"))
        elif ease_out:
            pieces.append(Piece(body_end, last, finish, IDENTITY, "smooth"))
    return [piece for piece in pieces if piece.last > piece.first]


def state_at(pieces: list[Piece], frame: int) -> tuple[float, float, float]:
    for piece in pieces:
        if piece.first <= frame < piece.last:
            return piece.state(frame)
    return IDENTITY


def _edge(piece: Piece, axis: int, sign: int) -> str:
    """One edge of the view, as a share of the frame, across a piece."""
    (s0, *begin), (s1, *end) = piece.begin, piece.end
    start = begin[axis] + sign * 0.5 / s0
    if piece.begin == piece.end:
        return f"{start:.7f}"
    span = max(1, piece.last - 1 - piece.first)
    prefix = f"st(0,clip((on-1-{piece.first})/{span},0,1));st(1,{_EASE_EXPRESSION[piece.curve]});"
    centre = f"{begin[axis]:.7f}+({end[axis] - begin[axis]:.7f})*ld(1)"
    return f"({prefix}{centre}+({sign * 0.5 / s0:.7f})*pow({s0 / s1:.8f},ld(1)))"


def _tree(pieces: list[Piece], axis: int, sign: int, rest: str) -> str:
    """A balanced choice on the frame number, which keeps FFmpeg's expression depth small."""
    if not pieces:
        return rest
    if len(pieces) == 1:
        piece = pieces[0]
        return f"if(between(on-1,{piece.first},{piece.last - 1}),{_edge(piece, axis, sign)},{rest})"
    middle = len(pieces) // 2
    return f"if(lt(on-1,{pieces[middle].first}),{_tree(pieces[:middle], axis, sign, rest)},{_tree(pieces[middle:], axis, sign, rest)})"


def zoom_filter(pieces: list[Piece]) -> str | None:
    """The ``perspective`` filter for planned pieces, or None when the frame never moves."""
    active = [piece for piece in pieces if piece.begin != IDENTITY or piece.end != IDENTITY]
    if not active:
        return None
    left, right = _tree(active, 0, -1, "0"), _tree(active, 0, 1, "1")
    top, bottom = _tree(active, 1, -1, "0"), _tree(active, 1, 1, "1")
    corners = {
        "x0": f"W*({left})",
        "y0": f"H*({top})",
        "x1": f"W*({right})",
        "y1": f"H*({top})",
        "x2": f"W*({left})",
        "y2": f"H*({bottom})",
        "x3": f"W*({right})",
        "y3": f"H*({bottom})",
    }
    spans: list[list[int]] = []
    for piece in active:
        if spans and spans[-1][1] >= piece.first:
            spans[-1][1] = max(spans[-1][1], piece.last)
        else:
            spans.append([piece.first, piece.last])
    # Untouched frames skip the resampler entirely, and stay byte-identical.
    window = "+".join(f"between(n,{first},{last - 1})" for first, last in spans)
    options = ":".join(f"{key}='{value}'" for key, value in corners.items())
    return f"perspective={options}:interpolation=cubic:sense=source:eval=frame:enable='{window}'"


def _windows(view: dict[str, Any]) -> dict[int, tuple[float, float]]:
    windows: dict[int, tuple[float, float]] = {}
    for span in view["spans"]:
        first = windows.get(span.segment, (span.output_start, span.output_end))
        windows[span.segment] = (first[0], span.output_end)
    return windows


def zoom_moves(view: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Moves for a resolved timeline: a segment's ``motion`` first, then the zoom mode.

    ``slow`` pushes in on every moment. Across a transition, where a push-in
    would have to fall back to the start mid-dissolve, moments take turns
    pushing in and easing back out, so the picture is continuous. ``punch``
    holds a tighter frame on every second moment, landing most of the way there
    on the cut and settling the rest over a few frames.
    """
    mode, strength = settings.get("mode", "none"), float(settings.get("strength", 0.5))
    overlapped = float(view.get("transition_seconds") or 0.0) > 0
    windows = _windows(view)
    base = {"scale": 1.0, "x": 0.5, "y": 0.5}
    pushed = {**base, "scale": round(1 + SLOW_ZOOM * strength, 4)}
    punched = round(1 + PUNCH_ZOOM * strength, 4)
    moves: list[dict[str, Any]] = []
    for index, segment in enumerate(view["segments"], 1):
        if index not in windows:
            continue
        start, end = windows[index]
        window = {"start": round(start, 6), "end": round(end, 6)}
        motion = segment.get("motion")
        if isinstance(motion, dict):
            moves.append(
                {**window, "from": motion.get("from") or base, "to": motion.get("to") or base, "ease": "smooth"}
            )
        elif mode == "slow" and strength > 0:
            back = overlapped and index % 2 == 0
            moves.append({**window, "from": pushed if back else base, "to": base if back else pushed, "ease": "gentle"})
        elif mode == "punch" and strength > 0 and index % 2 == 0:
            held = {**base, "scale": punched}
            moves.append({**window, "from": held, "to": held, "settle": PUNCH_SECONDS})
    return moves


_LOCAL_MOTION = re.compile(r"\(LM (-?\d+) (-?\d+) (-?\d+) (-?\d+) (-?\d+) ")


def scale_transforms(text: str, factor: int) -> str:
    """Scale an ASCII vid.stab file up to the frame it will be applied to.

    Detection is five times faster at half size and loses nothing measurable, but
    its motions are in half-size pixels.
    """
    return _LOCAL_MOTION.sub(lambda m: "(LM " + " ".join(str(int(m[i]) * factor) for i in range(1, 6)) + " ", text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vedit.motion", description="Scale a half-size vid.stab transform file")
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--factor", type=int, required=True)
    args = parser.parse_args(argv)
    Path(args.out).write_text(
        scale_transforms(Path(args.source).read_text(encoding="utf-8"), args.factor), encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
