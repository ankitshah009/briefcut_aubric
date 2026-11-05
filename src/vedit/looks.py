"""Looks as data, and the color math that turns them into a 3D LUT.

A look is a recipe of plain numbers. ``grade`` evaluates one for a single RGB
color and ``write_cube`` samples that function into a ``.cube`` file, so all of
a look's color work runs as one ``lut3d`` filter. Three things follow from that:

* The filter is a static file reference, so ``auto`` can measure the finished
  tape during the render and the FFmpeg command still reads the same.
* ``intensity`` is an exact blend with the untouched picture.
* Every look shares one path through 16-bit RGB with an explicit BT.709 matrix.
  The 8-bit round trip FFmpeg negotiates by itself costs 20 dB of PSNR on an
  identity LUT, and it converts RGB back with BT.601 unless told otherwise,
  while the export declares BT.709.

Recipe fields, all optional:

=============  ================================================================
``curve``      ``[[in, out], ...]`` tone curve, monotone between points.
``curve_mix``  0 moves luma alone, 1 curves each channel, which adds film-like
               saturation where the curve is steep.
``exposure``   Stops of gain, with a soft shoulder instead of clipping.
``gamma``      Midtone gamma after exposure. Below 1 lifts midtones.
``levels``     ``[black, white]`` input points, stretched to 0 and 1.
``temperature``  -1 cool to 1 warm.
``tint``       -1 green to 1 magenta.
``gains``      ``[r, g, b]`` measured white balance.
``saturation``  Added to a chroma gain of 1.
``vibrance``   Like saturation, but weighted towards muted colors.
``skin``       0 to 1, how much of the color work skin tones are spared.
``bands``      ``[[angle, width, rotate, gain], ...]`` hue moves on the
               vectorscope, in degrees. ``gain`` multiplies chroma.
``shadows``, ``highlights``  ``[r, g, b]`` tints, kept free of luma.
``mono``       ``[r, g, b]`` weights for a black and white mix.
``grain``, ``vignette``, ``sharpen``  The spatial parts, 0 to 1.
=============  ================================================================

This module only reads files when measuring footage for ``auto``. The compiler
is still the one thing that turns any of this into executable steps.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from functools import lru_cache
from itertools import pairwise
from pathlib import Path
from typing import Any

from vedit.media import filter_value, has_filter, probe, resolve_ffmpeg

LUT_SIZE = 33
SKIN_ANGLE = 123.0
SKIN_WIDTH = 28.0
_KR, _KG, _KB = 0.2126, 0.7152, 0.0722
_CB_SCALE, _CR_SCALE = 2 * (1 - _KB), 2 * (1 - _KR)
_GAMMA = 2.2
_CURVE_STEPS = 4096
# Linear-light gain per stop, expressed on gamma-coded values.
_STOP = 2 ** (1 / _GAMMA)

# One grain unit of FFmpeg's noise filter, at full strength.
_GRAIN_MAX = 14
# Lens angle at full strength. The filter scales luma, black offset included, so
# corners fall well below cos(angle)^4 of their light. Past this they read as black.
_VIGNETTE_MAX = math.pi / 5
_SHARPEN_MAX = 0.75

FILM_SHOULDER = [[0.0, 0.0], [0.72, 0.72], [0.9, 0.86], [1.0, 0.94]]

RECIPES: dict[str, dict[str, Any]] = {
    "none": {},
    "auto": {},
    "warm": {
        "temperature": 0.32,
        "tint": 0.04,
        "curve": [[0.0, 0.0], [0.25, 0.235], [0.55, 0.57], [0.85, 0.87], [1.0, 0.985]],
        "curve_mix": 0.5,
        "vibrance": 0.12,
        "skin": 0.8,
        "highlights": [0.018, 0.006, -0.02],
        "shadows": [0.006, 0.0, -0.008],
        "bands": [[175.0, 34.0, -8.0, 1.06], [230.0, 40.0, -6.0, 0.96]],
    },
    "cool": {
        "temperature": -0.28,
        "tint": -0.02,
        "curve": [[0.0, 0.0], [0.22, 0.2], [0.5, 0.505], [0.8, 0.825], [1.0, 1.0]],
        "curve_mix": 0.5,
        "saturation": -0.04,
        "vibrance": 0.08,
        "skin": 0.85,
        "shadows": [-0.012, 0.0, 0.018],
        "highlights": [-0.004, 0.002, 0.008],
        "bands": [[175.0, 36.0, 0.0, 0.86], [350.0, 40.0, 0.0, 1.08]],
    },
    "vivid": {
        "curve": [[0.0, 0.0], [0.2, 0.17], [0.5, 0.515], [0.8, 0.84], [1.0, 1.0]],
        "curve_mix": 0.65,
        "saturation": 0.05,
        "vibrance": 0.34,
        "skin": 0.8,
        "bands": [[230.0, 34.0, 0.0, 1.06], [340.0, 44.0, 0.0, 1.08]],
    },
    "cinematic": {
        "curve": [[0.0, 0.018], [0.18, 0.135], [0.45, 0.455], [0.78, 0.8], [1.0, 0.965]],
        "curve_mix": 0.55,
        "temperature": 0.05,
        "saturation": -0.05,
        "vibrance": 0.08,
        "skin": 0.9,
        "shadows": [-0.03, 0.002, 0.024],
        "highlights": [0.024, 0.006, -0.026],
        # Green walks towards teal, blue back towards it, yellow towards orange.
        "bands": [
            [225.0, 42.0, 18.0, 0.74],
            [345.0, 42.0, -14.0, 0.9],
            [176.0, 30.0, -10.0, 0.96],
            [100.0, 22.0, 7.0, 0.95],
        ],
        "vignette": 0.26,
        "grain": 0.16,
    },
    "film": {
        "curve": [[0.0, 0.052], [0.2, 0.2], [0.5, 0.5], [0.8, 0.79], [1.0, 0.94]],
        "curve_mix": 0.85,
        "temperature": 0.07,
        "saturation": -0.08,
        "vibrance": 0.04,
        "skin": 0.7,
        "shadows": [-0.012, 0.008, 0.006],
        "highlights": [0.012, 0.004, -0.014],
        "bands": [[225.0, 40.0, 10.0, 0.86], [345.0, 42.0, -8.0, 0.92]],
        "grain": 0.42,
        "vignette": 0.1,
    },
    "mono": {
        "mono": [0.3, 0.58, 0.12],
        "curve": [[0.0, 0.0], [0.22, 0.2], [0.5, 0.51], [0.8, 0.82], [1.0, 1.0]],
    },
    "noir": {
        "mono": [0.42, 0.5, 0.08],
        "curve": [[0.0, 0.0], [0.14, 0.045], [0.42, 0.36], [0.72, 0.78], [0.92, 0.965], [1.0, 1.0]],
        "vignette": 0.42,
        "grain": 0.3,
    },
    "vintage": {
        "curve": [[0.0, 0.07], [0.25, 0.25], [0.55, 0.57], [0.82, 0.81], [1.0, 0.925]],
        "curve_mix": 0.7,
        "temperature": 0.16,
        "saturation": -0.2,
        "skin": 0.5,
        "shadows": [0.022, 0.0, 0.006],
        "highlights": [0.026, 0.012, -0.03],
        "bands": [[225.0, 40.0, -10.0, 0.82], [345.0, 44.0, -18.0, 0.8], [176.0, 30.0, -6.0, 1.04]],
        "grain": 0.26,
        "vignette": 0.2,
    },
}


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return low if value < low else min(value, high)


def _smooth(low: float, high: float, value: float) -> float:
    t = _clip((value - low) / (high - low))
    return t * t * (3 - 2 * t)


def _monotone(points: list[list[float]]) -> list[float]:
    """Sample a monotone cubic (Fritsch and Carlson) through ``points`` on 0 to 1."""
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    slopes = [(ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]) for i in range(len(xs) - 1)]
    tangents = [slopes[0], *(0.0 if a * b <= 0 else 2 * a * b / (a + b) for a, b in pairwise(slopes)), slopes[-1]]
    table = []
    segment = 0
    for step in range(_CURVE_STEPS + 1):
        x = step / _CURVE_STEPS
        while segment < len(slopes) - 1 and x > xs[segment + 1]:
            segment += 1
        width = xs[segment + 1] - xs[segment]
        t = _clip((x - xs[segment]) / width)
        t2, t3 = t * t, t * t * t
        table.append(
            (2 * t3 - 3 * t2 + 1) * ys[segment]
            + (t3 - 2 * t2 + t) * width * tangents[segment]
            + (3 * t2 - 2 * t3) * ys[segment + 1]
            + (t3 - t2) * width * tangents[segment + 1]
        )
    return table


_KNEE = 0.8


def _shoulder_bend(peak: float) -> float:
    """How hard to bend so a roll-off from the knee leaves at slope 1 and lands ``peak`` on white."""
    wanted = (peak - _KNEE) / (1 - _KNEE)
    low, high = 1e-6, 64.0
    for _ in range(48):
        middle = (low + high) / 2
        if middle / (1 - math.exp(-middle)) < wanted:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def _shoulder(value: float, peak: float, bend: float) -> float:
    """Roll highlights off above the knee, so gain never clips hard. Nothing past white, nothing changes."""
    if peak <= 1 or value <= _KNEE:
        return value
    return _KNEE + (1 - _KNEE) * (1 - math.exp(-bend * (value - _KNEE) / (peak - _KNEE))) / (1 - math.exp(-bend))


class _Prepared:
    """A recipe with its tables built, so a whole LUT reuses them."""

    def __init__(self, recipe: dict[str, Any]) -> None:
        self.recipe = recipe
        self.mono = recipe.get("mono")
        self.mix = float(recipe.get("curve_mix", 0.5))
        self.saturation = float(recipe.get("saturation", 0.0))
        self.vibrance = float(recipe.get("vibrance", 0.0))
        self.skin = float(recipe.get("skin", 0.0))
        self.bands = [tuple(map(float, band)) for band in recipe.get("bands", ())]
        self.shadows = self._chroma_only(recipe.get("shadows"))
        self.highlights = self._chroma_only(recipe.get("highlights"))
        self.gains = self._gains(recipe)
        black, white = recipe.get("levels") or (0.0, 1.0)
        exposure = float(recipe.get("exposure", 0.0))
        gamma = float(recipe.get("gamma", 1.0))
        gain = _STOP**exposure / (white - black)
        # Where the brightest input lands decides how much room the shoulder needs.
        peak = (1 - black) * gain
        bend = _shoulder_bend(peak) if peak > 1 else 0.0
        curve = _monotone(recipe["curve"]) if recipe.get("curve") else None
        table = []
        for step in range(_CURVE_STEPS + 1):
            value = _shoulder(max(0.0, (step / _CURVE_STEPS - black) * gain), peak, bend)
            if gamma != 1.0:
                value = _clip(value) ** gamma
            if curve is not None:
                value = curve[round(_clip(value) * _CURVE_STEPS)]
            table.append(_clip(value))
        self.table = table
        self.identity_tone = all(abs(v - i / _CURVE_STEPS) < 1e-9 for i, v in enumerate(table))

    @staticmethod
    def _chroma_only(tint: Any) -> tuple[float, float, float] | None:
        if not tint:
            return None
        r, g, b = map(float, tint)
        luma = _KR * r + _KG * g + _KB * b
        return r - luma, g - luma, b - luma

    @staticmethod
    def _gains(recipe: dict[str, Any]) -> tuple[float, float, float] | None:
        temperature = float(recipe.get("temperature", 0.0))
        tint = float(recipe.get("tint", 0.0))
        measured = recipe.get("gains")
        if not temperature and not tint and not measured:
            return None
        r = 1 + 0.11 * temperature
        b = 1 - 0.16 * temperature
        g = 1 - 0.09 * tint
        if measured:
            r, g, b = r * measured[0], g * measured[1], b * measured[2]
        # White balance moves color, not brightness.
        scale = 1 / (_KR * r + _KG * g + _KB * b)
        return r * scale, g * scale, b * scale

    def tone(self, value: float) -> float:
        return self.table[round(_clip(value) * _CURVE_STEPS)]


def _hue_work(p: _Prepared, cb: float, cr: float) -> tuple[float, float, float, float]:
    """Spare skin tones and move the hue bands: the new ``cb, cr, chroma`` and how much of this color is skin."""
    chroma = math.hypot(cb, cr)
    skin = 0.0
    if chroma <= 1e-5 or not (p.skin or p.bands):
        return cb, cr, chroma, skin
    angle = math.degrees(math.atan2(cr, cb)) % 360
    if p.skin:
        away = abs((angle - SKIN_ANGLE + 180) % 360 - 180)
        if away < SKIN_WIDTH:
            near = 1 - (away / SKIN_WIDTH) ** 2
            skin = p.skin * near * near * _smooth(0.012, 0.05, chroma) * (1 - _smooth(0.3, 0.46, chroma))
    gain, turn = 1.0, 0.0
    for center, width, rotate, band_gain in p.bands:
        away = abs((angle - center + 180) % 360 - 180)
        if away < width:
            near = 1 - (away / width) ** 2
            near *= near
            turn += rotate * near
            gain *= 1 + (band_gain - 1) * near
    keep = 1 - skin
    turn *= keep
    gain = 1 + (gain - 1) * keep
    if turn or gain != 1:
        radians = math.radians(angle + turn)
        chroma *= gain
        cb, cr = chroma * math.cos(radians), chroma * math.sin(radians)
    return cb, cr, chroma, skin


def _graded(r: float, g: float, b: float, p: _Prepared) -> tuple[float, float, float]:
    if p.gains is not None:
        r, g, b = _clip(r * p.gains[0]), _clip(g * p.gains[1]), _clip(b * p.gains[2])

    if p.mono is not None:
        value = p.tone(_clip(p.mono[0] * r + p.mono[1] * g + p.mono[2] * b))
        r, g, b = value, value, value
    elif not p.identity_tone:
        luma = _KR * r + _KG * g + _KB * b
        lift = p.tone(luma) - luma
        r += lift + p.mix * (p.tone(r) - r - lift)
        g += lift + p.mix * (p.tone(g) - g - lift)
        b += lift + p.mix * (p.tone(b) - b - lift)

    luma = _clip(_KR * r + _KG * g + _KB * b)
    cb, cr = (b - luma) / _CB_SCALE, (r - luma) / _CR_SCALE
    cb, cr, chroma, skin = _hue_work(p, cb, cr)
    wanted = p.saturation + p.vibrance * (1 - _clip(chroma / 0.32))
    if wanted:
        wanted *= 1 - skin
        cb, cr = cb * (1 + wanted), cr * (1 + wanted)

    # Rebuild around the same luma, then spend the tints.
    r, b = luma + _CR_SCALE * cr, luma + _CB_SCALE * cb
    g = (luma - _KR * r - _KB * b) / _KG
    keep = 1 - skin * 0.75
    if p.shadows is not None:
        weight = keep * (1 - _smooth(0.0, 0.55, luma))
        r, g, b = r + p.shadows[0] * weight, g + p.shadows[1] * weight, b + p.shadows[2] * weight
    if p.highlights is not None:
        weight = keep * _smooth(0.4, 0.95, luma)
        r, g, b = r + p.highlights[0] * weight, g + p.highlights[1] * weight, b + p.highlights[2] * weight

    # Bring anything out of gamut back along its own hue. Clipping each channel
    # alone is what turns a blown sky cyan.
    r, g, b = r - luma, g - luma, b - luma
    scale = 1.0
    for offset in (r, g, b):
        if offset > 0 and luma + offset > 1:
            scale = min(scale, (1 - luma) / offset)
        elif offset < 0 and luma + offset < 0:
            scale = min(scale, luma / -offset)
    return _clip(luma + r * scale), _clip(luma + g * scale), _clip(luma + b * scale)


def grade(
    r: float, g: float, b: float, recipe: dict[str, Any], *, intensity: float = 1.0
) -> tuple[float, float, float]:
    """One gamma-coded BT.709 color through a recipe. Pure, and what the LUT samples."""
    return _blend(r, g, b, _prepared(recipe), intensity)


def _blend(r: float, g: float, b: float, p: _Prepared, intensity: float) -> tuple[float, float, float]:
    nr, ng, nb = _graded(r, g, b, p)
    if intensity >= 1:
        return nr, ng, nb
    return r + (nr - r) * intensity, g + (ng - g) * intensity, b + (nb - b) * intensity


@lru_cache(maxsize=32)
def _prepared_from(blob: str) -> _Prepared:
    return _Prepared(json.loads(blob))


def _prepared(recipe: dict[str, Any]) -> _Prepared:
    return _prepared_from(json.dumps(recipe, sort_keys=True))


def slider_recipe(look: dict[str, Any]) -> dict[str, Any]:
    """The manual sliders as a recipe of their own, which sits on top of any preset."""
    brightness = float(look.get("brightness") or 0.0)
    contrast = float(look.get("contrast") or 0.0)
    saturation = float(look.get("saturation") or 0.0)
    temperature = float(look.get("temperature") or 0.0)
    sliders: dict[str, Any] = {}
    if brightness:
        sliders["exposure"] = brightness * 1.4
    if contrast:
        sliders["curve"] = _contrast_curve(contrast)
        sliders["curve_mix"] = 0.6
    if saturation:
        sliders["saturation"] = saturation if saturation < 0 else saturation * 0.8
    if temperature:
        sliders["temperature"] = temperature
    return sliders


def _contrast_curve(amount: float) -> list[list[float]]:
    points = []
    for step in range(9):
        x = step / 8
        points.append([x, _clip(x + amount * 0.145 * math.sin(2 * math.pi * (x - 0.5)))])
    return points


def color_recipes(look: dict[str, Any], measured: dict[str, Any] | None = None) -> list[tuple[dict[str, Any], float]]:
    """The color work a look asks for, as ``(recipe, strength)`` in order.

    ``intensity`` is how strongly the preset applies. The sliders always apply in
    full, or turning a look down would quietly turn someone's own settings down too.
    An empty list means leave color alone.
    """
    preset = str(look.get("preset") or "none")
    if preset not in RECIPES:
        raise ValueError(f"unknown look: {preset}")
    base = dict(RECIPES[preset])
    if preset == "auto" and measured:
        base.update(measured)
    for key in ("grain", "vignette", "sharpen"):
        base.pop(key, None)
    intensity = _clip(float(look.get("intensity", 1.0)))
    steps = [(base, intensity), (slider_recipe(look), 1.0)]
    return [(recipe, strength) for recipe, strength in steps if recipe and strength > 0]


def spatial_amounts(look: dict[str, Any]) -> dict[str, float]:
    """Grain, vignette, and sharpen on 0 to 1, from the preset and the sliders."""
    preset = RECIPES.get(str(look.get("preset") or "none"), {})
    intensity = _clip(float(look.get("intensity", 1.0)))
    return {
        "grain": _clip(float(preset.get("grain", 0.0)) * intensity),
        "vignette": _clip(float(preset.get("vignette", 0.0)) * intensity + float(look.get("vignette") or 0.0)),
        "sharpen": _clip(float(preset.get("sharpen", 0.0)) * intensity + float(look.get("sharpen") or 0.0)),
    }


def is_neutral(look: dict[str, Any]) -> bool:
    """True when a look changes nothing, so no filter should run."""
    auto = str(look.get("preset") or "none") == "auto" and float(look.get("intensity", 1.0)) > 0
    return not auto and not color_recipes(look) and not any(spatial_amounts(look).values())


def cube_text(steps: list[tuple[dict[str, Any], float]], *, size: int = LUT_SIZE, title: str = "Briefcut look") -> str:
    """Sample a look into ``.cube`` text. Red changes fastest, as the format requires."""
    prepared = [(_prepared(recipe), strength) for recipe, strength in steps]
    step = 1 / (size - 1)
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {size}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    for blue in range(size):
        for green in range(size):
            for red in range(size):
                r, g, b = red * step, green * step, blue * step
                for p, strength in prepared:
                    r, g, b = _blend(r, g, b, p, strength)
                lines.append(f"{r:.6f} {g:.6f} {b:.6f}")
    return "\n".join(lines) + "\n"


def write_cube(path: str | Path, steps: list[tuple[dict[str, Any], float]], *, size: int = LUT_SIZE) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(cube_text(steps, size=size), encoding="ascii")
    return dest


def lut_filters(path: str | Path) -> list[str]:
    """The LUT between explicit BT.709 conversions in 16-bit RGB. Round trips to the same YUV."""
    quality = "flags=accurate_rnd+full_chroma_int"
    return [
        f"scale=in_color_matrix=bt709:{quality}",
        "format=gbrp16le",
        f"lut3d=file={filter_value(path)}:interp=tetrahedral",
        f"scale=out_color_matrix=bt709:out_range=limited:{quality}",
        "format=yuv420p",
    ]


def spatial_filters(amounts: dict[str, float], *, warn: Any = None) -> list[str]:
    """Sharpen, then vignette, then grain, so grain itself is never sharpened or shaded."""
    filters: list[str] = []
    note = warn or (lambda message: None)
    if amounts.get("sharpen", 0) > 0:
        strength = round(amounts["sharpen"] * _SHARPEN_MAX, 3)
        if has_filter("cas"):
            filters.append(f"cas=strength={strength}")
        elif has_filter("unsharp"):
            filters.append(f"unsharp=lx=5:ly=5:la={round(strength * 1.2, 3)}")
        else:
            note("This FFmpeg has no sharpening filter, so sharpen was skipped.")
    if amounts.get("vignette", 0) > 0:
        if has_filter("vignette"):
            # Stay round: the filter's aspect option pushes the corners past its
            # normalized distance, and they turn solid black.
            filters.append(f"vignette=angle={round(amounts['vignette'] ** 0.6 * _VIGNETTE_MAX, 4)}")
        else:
            note("This FFmpeg has no vignette filter, so the vignette was skipped.")
    if amounts.get("grain", 0) > 0:
        if has_filter("noise"):
            filters.append(f"noise=c0s={max(1, round(amounts['grain'] * _GRAIN_MAX))}:c0f=t+u")
        else:
            note("This FFmpeg has no noise filter, so the grain was skipped.")
    return filters


def fallback_filters(steps: list[tuple[dict[str, Any], float]]) -> list[str]:
    """A plain ``eq`` stand-in for machines without ``lut3d``."""
    samples = [(0.25, 0.25, 0.25), (0.5, 0.5, 0.5), (0.75, 0.75, 0.75)]
    prepared = [(_prepared(recipe), strength) for recipe, strength in steps]

    def through(color: tuple[float, float, float]) -> float:
        r, g, b = color
        for p, strength in prepared:
            r, g, b = _blend(r, g, b, p, strength)
        return _KR * r + _KG * g + _KB * b

    low, mid, high = (through(sample) for sample in samples)
    contrast = _clip((high - low) / 0.5, 0.5, 1.6)
    saturation = 1.0
    for recipe, strength in steps:
        if recipe.get("mono"):
            saturation *= 1 - strength
        saturation *= 1 + (float(recipe.get("saturation", 0.0)) + 0.5 * float(recipe.get("vibrance", 0.0))) * strength
    if not has_filter("eq"):
        return []
    return [
        f"eq=contrast={contrast:.3f}:brightness={_clip(mid - 0.5, -0.4, 0.4):.3f}:saturation={_clip(saturation, 0.0, 2.0):.3f}"
    ]


def _percentile(histogram: list[int], total: int, fraction: float) -> float:
    target = fraction * total
    seen = 0
    for level, count in enumerate(histogram):
        seen += count
        if seen >= target:
            return level / (len(histogram) - 1)
    return 1.0


def stats_from_pixels(pixels: bytes) -> dict[str, Any]:
    """Exposure and balance of RGB24 samples. Pure, so ``auto`` can be tested without FFmpeg."""
    bins = 256
    histogram = [0] * bins
    chroma_sum = 0.0
    total = len(pixels) // 3
    neutral_r = neutral_g = neutral_b = 0.0
    neutral = 0
    for index in range(0, total * 3, 3):
        r, g, b = pixels[index], pixels[index + 1], pixels[index + 2]
        luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
        histogram[int(luma + 0.5)] += 1
        spread = max(r, g, b) - min(r, g, b)
        chroma_sum += spread
        # White balance can only trust surfaces that are already nearly neutral,
        # so a green lawn or a blue sky never counts as a color cast.
        if 40 <= luma <= 225 and spread < 0.14 * luma + 6:
            neutral_r += r
            neutral_g += g
            neutral_b += b
            neutral += 1
    if not total:
        raise ValueError("no pixels to measure")
    stats: dict[str, Any] = {
        "low": _percentile(histogram, total, 0.02),
        "median": _percentile(histogram, total, 0.5),
        "high": _percentile(histogram, total, 0.98),
        "chroma": chroma_sum / total / 255,
        "neutral_share": neutral / total,
    }
    if neutral:
        stats["neutral"] = [neutral_r / neutral / 255, neutral_g / neutral / 255, neutral_b / neutral / 255]
    return stats


def auto_recipe(stats: dict[str, Any]) -> dict[str, Any]:
    """Derive a correction from measured statistics. Pure.

    Levels first, then midtones, so a dark clip is stretched into range before
    it is lifted. Each move has a dead zone and a limit, because a dim room or a
    snowfield can be exactly what the footage should look like. Well-exposed
    clips change very little and badly exposed ones change a lot.
    """
    low, median, high = float(stats["low"]), float(stats["median"]), float(stats["high"])
    black = _clip((low - 0.05) * 0.6, 0.0, 0.07) if low > 0.08 else 0.0
    # Healthy highlights stay put. Below 0.55 the frame is plainly underexposed.
    urgency = 1 - _smooth(0.55, 0.82, high)
    gain = min(1 + (0.92 / max(high, 0.05) - 1) * urgency, 2.0)
    white = max(black + (1 - black) / gain, black + 0.2)
    span = white - black
    median_after = _clip((median - black) / span, 0.01, 0.99)
    gamma = 1.0
    if median_after < 0.34:
        target = 0.34 + 0.09 * (median_after / 0.34) ** 2
        gamma = _clip(math.log(target) / math.log(median_after), 0.55, 1.0)
    elif median_after > 0.66:
        gamma = _clip(1 + (math.log(0.66) / math.log(median_after) - 1) * 0.5, 1.0, 1.3)
    recipe: dict[str, Any] = {}
    if black or abs(white - 1.0) > 1e-3:
        recipe["levels"] = [round(black, 4), round(white, 4)]
    if abs(gamma - 1.0) > 1e-3:
        recipe["gamma"] = round(gamma, 4)
    if recipe:
        # Gain belongs on each channel, so a lifted color stays as rich as it was.
        recipe["curve_mix"] = 0.9
    if stats.get("neutral") and float(stats.get("neutral_share", 0.0)) >= 0.08:
        r, g, b = stats["neutral"]
        grey = (r + g + b) / 3
        if grey > 0.02 and min(r, g, b) > 0:
            gains = [_clip(1 + (grey / value - 1) * 0.5, 0.95, 1.05) for value in (r, g, b)]
            if max(gains) - min(gains) > 0.02:
                recipe["gains"] = [round(value, 4) for value in gains]
    vibrance = _clip((0.12 - float(stats.get("chroma", 0.2))) * 1.0, 0.0, 0.12)
    if vibrance > 0.02:
        recipe["vibrance"] = round(vibrance, 4)
        recipe["skin"] = 0.8
    # A little contrast once the range is healthy. Flat footage reads as washed out otherwise.
    spread = (high - low) * gain
    if spread < 0.7:
        recipe["curve"] = _contrast_curve(_clip((0.7 - spread) * 0.5, 0.0, 0.16))
        recipe.setdefault("curve_mix", 0.6)
    return recipe


def measure(path: str | Path, *, samples: int = 16, width: int = 96) -> dict[str, Any]:
    """Sample evenly spaced small frames of a clip and return their statistics."""
    source = Path(path)
    info = probe(source)
    if not info.has_video:
        raise ValueError(f"{source} has no picture to measure")
    frames = max(1, round(info.duration * (info.fps or 30.0)))
    every = max(1, frames // samples)
    # The footage, not cover art or a proxy stream that happens to come first.
    argv = [
        resolve_ffmpeg(),
        "-hide_banner",
        "-v",
        "error",
        "-i",
        str(source),
        "-map",
        f"0:v:{info.video_map}",
        "-an",
        "-vf",
        f"select='not(mod(n\\,{every}))',scale={width}:-2:flags=area:in_color_matrix=bt709,format=rgb24",
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(samples + 1),
        "-f",
        "rawvideo",
        "-",
    ]
    done = subprocess.run(argv, capture_output=True, timeout=600, check=False)
    if done.returncode != 0 or not done.stdout:
        raise ValueError(f"could not sample {source}: {done.stderr.decode('utf-8', 'replace')[-300:]}")
    return stats_from_pixels(done.stdout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vedit.looks", description="Measure footage and write the auto look")
    parser.add_argument("--in", dest="source", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--look", required=True, help="the look settings as JSON")
    args = parser.parse_args(argv)
    look = json.loads(args.look)
    measured = auto_recipe(measure(args.source))
    # Footage that needs nothing still has to leave a file for the next step to read.
    write_cube(args.out, color_recipes(look, measured) or [({}, 1.0)])
    Path(args.out).with_suffix(".json").write_text(
        json.dumps({"measured": measured}, indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
